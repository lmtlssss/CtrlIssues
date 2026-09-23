#!/usr/bin/env python3
"""Install a local CtrlIssues source tree or package without changing Codex itself."""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, sqlite3, stat, subprocess, sys, tempfile, zipfile
import queue, threading
import platform
from pathlib import Path, PurePosixPath, PureWindowsPath

NAME = "ctrlissues"
PLUGIN_ID = "ctrlissues@ctrlissues"
DATA_NAME = "ctrlissues-ctrlissues"
MAX_PACKAGE = 96 * 1024 * 1024
EXTRA_FILES = ("install.sh", "install.ps1", "scripts/install.py", "scripts/package.py",
               "scripts/validate-package.py", "scripts/prove-system.sh", "scripts/prove-system.ps1", "docs/install.md")
EXPECTED_HOOKS = {"SessionStart", "SubagentStart", "UserPromptSubmit", "PreCompact", "PreToolUse", "PostToolUse"}
TARGETS = {"x86_64-unknown-linux-musl", "aarch64-apple-darwin", "x86_64-apple-darwin", "x86_64-pc-windows-msvc", "local", "windows-msvc-local"}

def atomic(path: Path, body: bytes, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f: f.write(body); f.flush(); os.fsync(f.fileno())
        os.chmod(tmp, mode); os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def private_mkdir(path: Path):
    missing=[]; current=path
    while not current.exists():
        missing.append(current); current=current.parent
    if current.is_symlink() or not current.is_dir(): raise ValueError("unsafe_data_parent:"+str(current))
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)

def ordinary_env(codex_root: Path):
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "LANG", "LC_ALL", "LC_CTYPE", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "SSH_AUTH_SOCK", "DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY"}
    env = {k:v for k,v in os.environ.items() if k in allowed}
    env["CODEX_HOME"] = str(codex_root)
    return env

def codex(binary, env, *args):
    p = subprocess.run([binary, "-c", "features.hooks=true", "-c", "features.plugins=true", *args], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    if p.returncode: raise RuntimeError("codex " + " ".join(args[:3]) + " failed: " + p.stderr[-1200:])
    return p.stdout

def validate_contents(files: dict[str, bytes], meta: dict):
    if meta.get("format") != "ctrlissues.package.v1" or meta.get("name") != NAME or meta.get("version") != "0.1.0": raise ValueError("package_identity")
    if meta.get("target") not in TARGETS: raise ValueError("package_target")
    hashes = meta.get("files")
    if not isinstance(hashes, dict) or set(hashes) != set(files): raise ValueError("package_inventory")
    h = hashlib.sha256()
    for name, expected in sorted(hashes.items()):
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or "." in p.parts or "\\" in name or "\x00" in name or p.as_posix()!=name: raise ValueError("unsafe_package_path")
        body=files[name]
        if not isinstance(expected,str) or len(expected)!=64 or any(c not in "0123456789abcdef" for c in expected): raise ValueError("invalid_package_hash:"+name)
        if hashlib.sha256(body).hexdigest()!=expected: raise ValueError("package_hash:"+name)
        h.update(name.encode()+b"\0"+bytes.fromhex(expected))
    if h.hexdigest()!=meta.get("generation"): raise ValueError("package_generation")
    required={".agents/plugins/marketplace.json", "plugins/ctrlissues/.codex-plugin/plugin.json",
              "plugins/ctrlissues/hooks/hooks.json", "plugins/ctrlissues/backend/main.py",
              "plugins/ctrlissues/runtime/src/main.rs", "plugins/ctrlissues/skills/ctrlissues/SKILL.md",
              "plugins/ctrlissues/skills/ctrlissues/references/task.md",
              "plugins/ctrlissues/skills/ctrlissues/references/execution.md", *EXTRA_FILES}
    if not required.issubset(files): raise ValueError("package_files_missing")
    if "plugins/ctrlissues/.codex-plugin/plugin.json" not in files or "plugins/ctrlissues/hooks/hooks.json" not in files:
        raise ValueError("plugin_files_missing")
    manifest=json.loads(files["plugins/ctrlissues/.codex-plugin/plugin.json"])
    hooks=json.loads(files["plugins/ctrlissues/hooks/hooks.json"])
    market=json.loads(files[".agents/plugins/marketplace.json"])
    skill=files["plugins/ctrlissues/skills/ctrlissues/SKILL.md"].decode("utf-8")
    if manifest.get("name")!=NAME or manifest.get("version")!="0.1.0": raise ValueError("plugin_manifest")
    matches=[x for x in market.get("plugins",[]) if isinstance(x,dict) and x.get("name")==NAME]
    if len(matches)!=1 or matches[0].get("source")!={"source":"local","path":"./plugins/ctrlissues"}: raise ValueError("marketplace_entry")
    if not all(k in matches[0].get("policy",{}) for k in ("installation","authentication")) or not matches[0].get("category"): raise ValueError("marketplace_policy")
    if set(hooks.get("hooks",{}))!=EXPECTED_HOOKS: raise ValueError("hook_definitions")
    if "for agents with executive dysfunction." not in manifest.get("description","") or "for agents with executive dysfunction." not in skill: raise ValueError("approved_tagline")
    for ref in ("plugins/ctrlissues/skills/ctrlissues/references/task.md","plugins/ctrlissues/skills/ctrlissues/references/execution.md"):
        if ref not in files: raise ValueError("skill_reference:"+ref)
    binaries=[n for n in files if n in ("plugins/ctrlissues/runtime/bin/ctrlissues","plugins/ctrlissues/runtime/bin/ctrlissues.exe")]
    if len(binaries)!=1 or not files[binaries[0]]: raise ValueError("binary_missing")
    if binaries[0].endswith(".exe") != ("windows-msvc" in meta.get("target","")): raise ValueError("binary_target_mismatch")
    return meta

def unpack(package: Path, stage: Path):
    with zipfile.ZipFile(package) as z:
        infos=z.infolist()
        if len(infos)>1024 or sum(i.file_size for i in infos)>MAX_PACKAGE: raise ValueError("package_bound")
        seen=set(); members={}
        for i in infos:
            n=i.filename; p=PurePosixPath(n)
            if (not n or n.endswith("/") or p.is_absolute() or ".." in p.parts or "." in p.parts or p.as_posix()!=n
                    or "\\" in n or "\x00" in n or PureWindowsPath(n).drive or n in seen or stat.S_ISLNK(i.external_attr>>16)):
                raise ValueError("unsafe_archive_path")
            seen.add(n)
            if n!="ctrlissues-package.json": members[n]=z.read(i)
        if "ctrlissues-package.json" not in seen: raise ValueError("package_metadata")
        meta=json.loads(z.read("ctrlissues-package.json"))
    validate_contents(members,meta)
    for name, body in members.items():
        out=stage/name; out.parent.mkdir(parents=True,exist_ok=True); out.write_bytes(body)
    (stage/"ctrlissues-package.json").write_text(json.dumps(meta,sort_keys=True,separators=(",",":"))+"\n")
    return meta

def inventory(root: Path):
    meta_path=root/"ctrlissues-package.json"
    if meta_path.is_symlink() or not meta_path.is_file(): raise ValueError("package_metadata_missing")
    meta=json.loads(meta_path.read_text(encoding="utf-8"))
    members={}
    for name in meta.get("files",{}):
        p=PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or "\\" in name: raise ValueError("unsafe_package_path")
        f=root/name
        if not f.is_file() or f.is_symlink(): raise ValueError("package_file_missing:"+name)
        members[name]=f.read_bytes()
    return validate_contents(members,meta)

def collect_source(source: Path, binary: Path):
    source=source.resolve(); plugin=source/"plugins/ctrlissues"
    if not plugin.is_dir(): raise ValueError("plugin_root_missing")
    files={}
    market=source/".agents/plugins/marketplace.json"
    if not market.is_file() or market.is_symlink() or source not in market.resolve().parents: raise ValueError("marketplace_missing")
    files[".agents/plugins/marketplace.json"]=market.read_bytes()
    if plugin.is_symlink(): raise ValueError("unsafe_plugin_root")
    for path in plugin.rglob("*"):
        rel=path.relative_to(plugin).parts
        if any(p in {"target","__pycache__",".pytest_cache"} for p in rel): continue
        if path.is_symlink(): raise ValueError("unsafe_source_file:"+str(path))
        if path.is_dir(): continue
        if not path.is_file() or source not in path.resolve().parents: raise ValueError("unsafe_source_file:"+str(path))
        files[path.relative_to(source).as_posix()]=path.read_bytes()
    for name in EXTRA_FILES:
        path=source/name
        if not path.is_file() or path.is_symlink() or source not in path.resolve().parents: raise ValueError("source_file_missing:"+name)
        files[name]=path.read_bytes()
    if not binary.is_file() or binary.is_symlink(): raise ValueError("binary_missing_or_unsafe")
    target="windows-msvc-local" if os.name=="nt" else "local"
    binary_name="plugins/ctrlissues/runtime/bin/ctrlissues.exe" if os.name=="nt" else "plugins/ctrlissues/runtime/bin/ctrlissues"
    files[binary_name]=binary.read_bytes()
    hashes={n:hashlib.sha256(b).hexdigest() for n,b in sorted(files.items())}
    generation=hashlib.sha256()
    for name, digest in sorted(hashes.items()): generation.update(name.encode()+b"\0"+bytes.fromhex(digest))
    meta={"format":"ctrlissues.package.v1","name":NAME,"version":"0.1.0","target":target,"generation":generation.hexdigest(),"files":hashes}
    validate_contents(files,meta)
    return files,meta

def check_target(target: str, from_source=False):
    machine=platform.machine().lower()
    if os.name=="nt": allowed={"x86_64-pc-windows-msvc","windows-msvc-local"}
    elif sys.platform=="darwin":
        allowed={"aarch64-apple-darwin"} if machine in {"arm64","aarch64"} else {"x86_64-apple-darwin"} if machine in {"x86_64","amd64"} else set()
    elif sys.platform.startswith("linux"): allowed={"x86_64-unknown-linux-musl"} if machine in {"x86_64","amd64"} else set()
    else: allowed=set()
    if from_source and target in {"local","windows-msvc-local"}: return
    if target not in allowed:
        raise ValueError("package_target_incompatible:"+target)

def snapshot(path):
    if not path.exists(): return None
    if path.is_symlink() or not path.is_file(): raise ValueError("unsafe_existing_path:"+str(path))
    return path.read_bytes()

def confine_payload(data: Path, value: str):
    value=value.strip()
    rel=PurePosixPath(value)
    if rel.is_absolute() or len(rel.parts)!=2 or rel.parts[0]!="payloads" or len(rel.parts[1])!=64 or any(c not in "0123456789abcdef" for c in rel.parts[1]): raise ValueError("current_pointer_invalid")
    p=data/value
    if p.is_symlink() or not p.is_dir() or data.resolve() not in p.resolve().parents: raise ValueError("current_payload_invalid")
    return p

def rpc_hooks(binary, env, enable=True, cwd: Path|None=None):
    p=subprocess.Popen([binary,"-c","features.hooks=true","-c","features.plugins=true","app-server"],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding="utf-8",bufsize=1)
    messages=queue.Queue(maxsize=64)
    def read_messages():
        try:
            for line in p.stdout:
                if len(line)>2_000_000: messages.put({"error":"response_bound"}); return
                try: messages.put(json.loads(line),timeout=1)
                except (ValueError,queue.Full): return
        finally:
            try: messages.put(None,timeout=1)
            except queue.Full: pass
    threading.Thread(target=read_messages,daemon=True).start()
    seq=0
    def call(method,params):
        nonlocal seq
        seq+=1; p.stdin.write(json.dumps({"id":seq,"method":method,"params":params})+"\n"); p.stdin.flush()
        deadline=__import__("time").monotonic()+15
        while True:
            remaining=deadline-__import__("time").monotonic()
            if remaining<=0: raise RuntimeError("app_server_timeout:"+method)
            try: msg=messages.get(timeout=remaining)
            except queue.Empty: raise RuntimeError("app_server_timeout:"+method) from None
            if msg is None: raise RuntimeError("app_server_closed")
            if msg.get("error")=="response_bound": raise RuntimeError("app_server_response_bound")
            if msg.get("id")==seq:
                if "error" in msg: raise RuntimeError("app_server_rejected:"+method)
                return msg.get("result",{})
    try:
        call("initialize",{"clientInfo":{"name":NAME,"version":"0.1.0"},"capabilities":{"experimentalApi":True}})
        p.stdin.write(json.dumps({"method":"initialized","params":{}})+"\n"); p.stdin.flush()
        result=call("hooks/list",{"cwds":[str((cwd or Path.cwd()).resolve())]})
        hooks=[h for group in result.get("data",[]) for h in group.get("hooks",[]) if h.get("pluginId")==PLUGIN_ID]
        if not hooks: raise RuntimeError("ctrlissues_hooks_not_loaded")
        edits=[]
        for h in hooks:
            if not isinstance(h.get("key"),str) or not h.get("key") or not isinstance(h.get("currentHash"),str) or not h.get("currentHash"): raise RuntimeError("unknown_hook_schema")
            edits.append({"keyPath":"hooks.state."+json.dumps(h["key"]),"value":{"enabled":enable,"trusted_hash":h["currentHash"] if enable else None},"mergeStrategy":"replace"})
        call("config/batchWrite",{"edits":edits,"reloadUserConfig":True})
        return len(hooks)
    finally:
        p.terminate()
        try: p.wait(timeout=3)
        except subprocess.TimeoutExpired: p.kill(); p.wait()

def activate(binary, env, market: Path, expected: set[Path], on_mutation=None):
    raw=json.loads(codex(binary,env,"plugin","marketplace","list","--json"))
    entries=[x for x in raw.get("marketplaces",[]) if x.get("name")==NAME]
    if len(entries)>1: raise RuntimeError("duplicate_ctrlissues_marketplaces")
    if entries:
        src=entries[0].get("marketplaceSource") or {}
        if src.get("sourceType")!="local" or not isinstance(src.get("source"),str): raise RuntimeError("marketplace_owner_not_local")
        owner=Path(src["source"]).expanduser().resolve()
        if owner not in expected: raise RuntimeError("marketplace_owned_by_other_source")
        if owner==market.resolve():
            if on_mutation: on_mutation()
            codex(binary,env,"plugin","remove",PLUGIN_ID)
            codex(binary,env,"plugin","add",PLUGIN_ID)
            return rpc_hooks(binary,env,cwd=Path.cwd())
        if on_mutation: on_mutation()
        codex(binary,env,"plugin","marketplace","remove",NAME)
    elif on_mutation:
        on_mutation()
    codex(binary,env,"plugin","marketplace","add",str(market))
    codex(binary,env,"plugin","add",PLUGIN_ID)
    return rpc_hooks(binary,env,cwd=Path.cwd())

def migrate_graphfather(data: Path, codex_root: Path):
    target=data/"state.sqlite"
    source=codex_root/"plugins/data/the-graphfather-the-graphfather/state.sqlite"
    if target.exists() or not source.is_file(): return "skipped"
    data.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,tmp=tempfile.mkstemp(prefix=".migration.",dir=data); os.close(fd)
    try:
        src=sqlite3.connect("file:"+str(source)+"?mode=ro",uri=True); dst=sqlite3.connect(tmp)
        try: src.backup(dst)
        finally: dst.close(); src.close()
        os.chmod(tmp,0o600)
        try: os.link(tmp,target)
        except FileExistsError: return "skipped_target_exists"
        return "copied"
    finally: Path(tmp).unlink(missing_ok=True)

def install(source: Path|None, package: Path|None, binary: Path|None, codex_root: Path, prefix: Path, bindir: Path, codex_bin: str, no_activate: bool, migrate: bool):
    if source and (binary is None or not binary.is_file() or binary.is_symlink()): raise ValueError("binary_missing_or_unsafe")
    isolated=(prefix.resolve()!= (codex_root/"plugins"/"data"/DATA_NAME).resolve())
    if not no_activate and isolated: raise ValueError("custom_prefix_requires_no_activate")
    data=prefix.resolve()
    launcher=data/("ctrlissues.cmd" if os.name=="nt" else "ctrlissues")
    user_launcher=bindir/("ctrlissues.cmd" if os.name=="nt" else "ctrlissues")
    marker="# CtrlIssues stable launcher"; user_marker="# CtrlIssues user launcher"
    old_launcher=snapshot(launcher)
    if old_launcher and marker.encode() not in old_launcher: raise ValueError("refusing_unrelated_launcher")
    old_user_launcher=snapshot(user_launcher)
    if old_user_launcher and user_marker.encode() not in old_user_launcher: raise ValueError("refusing_unrelated_user_launcher")
    private_mkdir(data)
    payloads=data/"payloads"
    if payloads.is_symlink(): raise ValueError("unsafe_payload_root")
    private_mkdir(payloads)
    old=snapshot(data/"current")
    prev=confine_payload(data,old.decode().strip()) if old else None
    stage=Path(tempfile.mkdtemp(prefix=".stage-",dir=data))
    try:
        if package:
            meta=unpack(package,stage)
        elif source:
            files,meta=collect_source(source,binary)
            for name,body in files.items():
                out=stage/name; out.parent.mkdir(parents=True,exist_ok=True); out.write_bytes(body)
            (stage/"ctrlissues-package.json").write_text(json.dumps(meta,sort_keys=True,separators=(",",":"))+"\n")
        else: raise ValueError("source_or_package_required")
        check_target(meta["target"],from_source=source is not None)
        gen=meta["generation"]; target=payloads/gen
        if target.exists():
            if target.is_symlink(): raise ValueError("unsafe_existing_payload")
            existing=inventory(target)
            if existing.get("generation")!=gen: raise ValueError("existing_payload_corrupt")
        else: os.replace(stage,target)
        executable=target/("plugins/ctrlissues/runtime/bin/ctrlissues.exe" if "windows-msvc" in meta.get("target","") else "plugins/ctrlissues/runtime/bin/ctrlissues")
        if os.name!="nt": os.chmod(executable,0o755)
        stage=None
    finally:
        if stage and stage.exists(): shutil.rmtree(stage)
    import shlex
    if os.name=="nt":
        body=("@echo off\r\nrem "+marker+"\r\nset /p PAYLOAD=<\"%~dp0current\"\r\n\"%~dp0%PAYLOAD%\\plugins\\ctrlissues\\runtime\\bin\\ctrlissues.exe\" --data-dir \""+str(data)+"\" %*\r\n").encode()
        user_body=("@echo off\r\nrem "+user_marker+"\r\n\""+str(launcher)+"\" %*\r\n").encode()
    else:
        body=("#!/bin/sh\n"+marker+"\nD=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\nP=$(cat \"$D/current\")\ncase \"$P\" in payloads/*) G=${P#payloads/};; *) echo 'invalid CtrlIssues payload pointer' >&2; exit 2;; esac\n[ ${#G} -eq 64 ] || { echo 'invalid CtrlIssues generation' >&2; exit 2; }\ncase \"$G\" in *[!0123456789abcdef]*) echo 'invalid CtrlIssues generation' >&2; exit 2;; esac\nexec \"$D/$P/plugins/ctrlissues/runtime/bin/ctrlissues\" --data-dir "+shlex.quote(str(data))+" \"$@\"\n").encode()
        user_body=("#!/bin/sh\n"+user_marker+"\nexec "+shlex.quote(str(launcher))+" \"$@\"\n").encode()
    launcher.parent.mkdir(parents=True,exist_ok=True)
    # Atomic generation selection occurs only after the complete payload is in place.
    previous_pointer=old
    previous_file=snapshot(data/"previous")
    native_started=False
    previous_launcher=old_launcher; previous_user_launcher=old_user_launcher
    market_root=target
    migration="not_requested"
    try:
        bindir.mkdir(parents=True,exist_ok=True)
        atomic(launcher,body,0o700 if os.name!="nt" else 0o600)
        atomic(user_launcher,user_body,0o700 if os.name!="nt" else 0o600)
        if previous_pointer and prev and prev != target:
            atomic(data/"previous",previous_pointer,0o600)
        atomic(data/"current",("payloads/"+gen+"\n").encode(),0o600)
        migration=migrate_graphfather(data,codex_root) if migrate else "not_requested"
        hooks_trusted=0
        if not no_activate:
            env=ordinary_env(codex_root)
            def began_native_change():
                nonlocal native_started
                native_started=True
            hooks_trusted=activate(codex_bin,env,market_root,{market_root,prev.resolve() if prev else market_root},began_native_change)
        return {"status":"installed","generation":gen,"payload":str(target),"data_dir":str(data),"launcher":str(launcher),"user_launcher":str(user_launcher),"registered":not no_activate,"hooks_trusted":hooks_trusted,"global_hooks_effective":"not_changed","migration":migration}
    except BaseException as primary:
        failures=[]
        if native_started:
            try:
                env=ordinary_env(codex_root)
                if prev:
                    activate(codex_bin,env,prev,{prev,target})
                else:
                    try: rpc_hooks(codex_bin,env,enable=False)
                    except Exception as e: failures.append("hook untrust: "+str(e))
                    for args in (("plugin","remove",PLUGIN_ID),("plugin","marketplace","remove",NAME)):
                        try: codex(codex_bin,env,*args)
                        except Exception as e: failures.append("native removal: "+str(e))
            except Exception as e: failures.append("native rollback: "+str(e))
        try:
            if previous_pointer is None: (data/"current").unlink(missing_ok=True)
            else: atomic(data/"current",previous_pointer,0o600)
            if previous_file is None: (data/"previous").unlink(missing_ok=True)
            else: atomic(data/"previous",previous_file,0o600)
            if old_launcher is None: launcher.unlink(missing_ok=True)
            else: atomic(launcher,old_launcher,0o700)
            if old_user_launcher is None: user_launcher.unlink(missing_ok=True)
            else: atomic(user_launcher,old_user_launcher,0o700)
        except Exception as e: failures.append("local rollback: "+str(e))
        if failures: raise RuntimeError("install failed; rollback incomplete: "+"; ".join(failures)) from primary
        raise

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group(); g.add_argument("--source",type=Path); g.add_argument("--package",type=Path)
    p.add_argument("--binary",type=Path); p.add_argument("--codex-root","--codex-home",dest="codex_root",type=Path,default=Path(os.environ.get("CODEX_HOME",Path.home()/".codex")))
    p.add_argument("--prefix",type=Path); p.add_argument("--bin-dir",type=Path); p.add_argument("--codex",default="codex"); p.add_argument("--no-activate",action="store_true"); p.add_argument("--migrate-graphfather",action="store_true")
    p.add_argument("--rollback",action="store_true"); p.add_argument("--uninstall",action="store_true")
    a=p.parse_args(argv); root=a.codex_root.expanduser().resolve(); prefix=(a.prefix or root/"plugins/data"/DATA_NAME).expanduser().resolve()
    if a.rollback:
        cur_pointer=snapshot(prefix/"current"); previous_pointer=snapshot(prefix/"previous")
        if cur_pointer is None or previous_pointer is None: raise ValueError("rollback_payload_unavailable")
        cur=confine_payload(prefix,cur_pointer.decode().strip())
        prev=previous_pointer.decode().strip(); prev_root=confine_payload(prefix,prev)
        native=prefix==(root/"plugins"/"data"/DATA_NAME).resolve()
        env=ordinary_env(root) if native else None
        native_changed=False
        def began_rollback_change():
            nonlocal native_changed
            native_changed=True
        try:
            if native: activate(a.codex,env,prev_root,{cur,prev_root},began_rollback_change)
            atomic(prefix/"current",(prev+"\n").encode())
            atomic(prefix/"previous",("payloads/"+cur.name+"\n").encode())
        except BaseException as primary:
            failures=[]
            try:
                if cur_pointer is not None: atomic(prefix/"current",cur_pointer)
                if previous_pointer is not None: atomic(prefix/"previous",previous_pointer)
                if native and native_changed: activate(a.codex,env,cur,{cur,prev_root})
            except Exception as e: failures.append(str(e))
            if failures: raise RuntimeError("rollback failed; previous selection could not be restored: "+"; ".join(failures)) from primary
            raise
        print(json.dumps({"status":"rolled_back","generation":Path(prev).name})); return 0
    if a.uninstall:
        if prefix!=(root/"plugins/data"/DATA_NAME).resolve(): p.error("custom-prefix uninstall is unsupported")
        env=ordinary_env(root)
        raw=json.loads(codex(a.codex,env,"plugin","marketplace","list","--json"))
        entries=[x for x in raw.get("marketplaces",[]) if x.get("name")==NAME]
        if len(entries)>1: p.error("duplicate CtrlIssues marketplaces")
        if entries:
            marketplace=entries[0].get("marketplaceSource",{})
            if marketplace.get("sourceType")!="local" or not isinstance(marketplace.get("source"),str): p.error("existing marketplace owner is not local")
            owner=Path(marketplace["source"]).expanduser().resolve()
            allowed={confine_payload(prefix,(prefix/"current").read_text().strip())}
            if (prefix/"previous").exists(): allowed.add(confine_payload(prefix,(prefix/"previous").read_text().strip()))
            if owner not in allowed: p.error("existing marketplace owner differs")
            rpc_hooks(a.codex,env,enable=False)
            codex(a.codex,env,"plugin","remove",PLUGIN_ID)
            codex(a.codex,env,"plugin","marketplace","remove",NAME)
        for path,marker in ((prefix/("ctrlissues.cmd" if os.name=="nt" else "ctrlissues"),b"# CtrlIssues stable launcher"),
                            ((a.bin_dir or (Path.home()/"bin" if os.name=="nt" else Path.home()/".local/bin")).expanduser().resolve()/("ctrlissues.cmd" if os.name=="nt" else "ctrlissues"),b"# CtrlIssues user launcher")):
            current=snapshot(path)
            if current is not None and marker in current: path.unlink()
        print(json.dumps({"status":"uninstalled","state_retained":str(prefix)})); return 0
    if not (a.source or a.package) or (a.source and not a.binary): p.error("--source requires --binary; package contains its binary")
    default_bin=Path.home()/"bin" if os.name=="nt" else Path.home()/".local/bin"
    result=install(a.source.resolve() if a.source else None,a.package.resolve() if a.package else None,a.binary.resolve() if a.binary else None,root,prefix,(a.bin_dir or default_bin).expanduser().resolve(),a.codex,a.no_activate,a.migrate_graphfather)
    print(json.dumps(result,sort_keys=True)); return 0
if __name__=="__main__":
    try: raise SystemExit(main())
    except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired,zipfile.BadZipFile) as e: print("CtrlIssues install stopped: "+str(e),file=sys.stderr); raise SystemExit(1)
