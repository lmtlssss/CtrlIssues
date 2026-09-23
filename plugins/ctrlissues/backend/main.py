"""Optional foreground typed work backend for CtrlIssues.

This module owns only backend-prefixed rows in the core database.  It does not
capture authority from local prompts, hooks, or private Codex configuration.
"""
from __future__ import annotations

import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

SCHEMAS = {"ctrlissues.work.v1", "jmt.work.v1"}
KINDS = {"observe", "action", "check", "decision"}
ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
MAX_WORK = 64
MAX_OUTPUT = 16384
DEFAULTS = {"max_parallel": 4, "max_rounds": 16, "max_executions": 64,
            "max_model_calls": 8, "timeout_seconds": 60}
CONTROL_ENV = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP",
               "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "SSL_CERT_FILE", "SSL_CERT_DIR",
               "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSH_AUTH_SOCK", "SSH_AGENT_PID", "KRB5CCNAME",
               "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "DISPLAY",
               "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "VIRTUAL_ENV", "CONDA_PREFIX",
               "PYTHONPATH", "PYTHONHOME", "ENVSTACK_CONFIG", "ENVSTACK_MOUNT"}


def dump(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


@lru_cache(maxsize=1)
def load_semantics():
    path = Path(__file__).with_name("jmt.py")
    spec = importlib.util.spec_from_file_location("ctrlissues_semantics", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, module._load_sibling("selection")


def private_dir(path):
    if not path.exists():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        try: path.chmod(0o700)
        except OSError: pass
    return path


def plugin_root():
    candidates = [os.environ.get("CTRLISSUES_PLUGIN_ROOT"),
                  os.environ.get("PLUGIN_ROOT"), str(Path(__file__).resolve().parents[1])]
    for item in candidates:
        if item and (Path(item) / "backend" / "main.py").is_file():
            return Path(item).resolve()
    raise RuntimeError("backend_plugin_root_unavailable")


def core_status():
    """Read the authoritative cursor once, with a clean non-provider env."""
    data, exe, session = (os.environ.get("CTRLISSUES_DATA"),
                          os.environ.get("CTRLISSUES_EXE"),
                          os.environ.get("CTRLISSUES_SESSION"))
    if not data or not exe or not session:
        return None, "core_locator_unavailable"
    env = {k: v for k, v in os.environ.items() if k in CONTROL_ENV}
    try:
        p = subprocess.run([exe, "--data-dir", data, "--session", session,
                            "status", "--compact"], env=env, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=5, check=False, shell=False)
        if p.returncode:
            return None, "core_status_failed"
        value = json.loads(p.stdout)
        if not isinstance(value, dict) or value.get("schema") != "ctrlissues.cursor.v1":
            return None, "core_status_invalid"
        return value, None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None, "core_status_unavailable"


def load_input(path):
    raw = sys.stdin.read(262145) if path == "-" else Path(path).read_text(encoding="utf-8")
    if len(raw.encode()) > 262144:
        raise ValueError("input_bound")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("input_object_required")
    return value, dump(value)


def validate(payload, cursor):
    if payload.get("schema") not in SCHEMAS:
        raise ValueError("invalid_schema")
    ident, phase = payload.get("id"), payload.get("phase")
    if not isinstance(ident, str) or not ID.fullmatch(ident) or ident == "abstain":
        raise ValueError("invalid_work_id")
    if phase not in {"observe", "build", "prove", "repair"}:
        raise ValueError("invalid_phase")
    context = payload.get("context")
    if not isinstance(context, dict) or not isinstance(context.get("request"), str) or not context["request"].strip():
        raise ValueError("pinned_context_request_required")
    if len(context["request"]) > 6000 or ("cwd" in context and not isinstance(context["cwd"], str)):
        raise ValueError("invalid_context")
    constraints = context.get("constraints", [])
    if not isinstance(constraints, list) or len(constraints) > 32 or any(not isinstance(x, str) or len(x) > 2000 for x in constraints):
        raise ValueError("invalid_context_constraints")
    if cursor:
        for key in ("session_id", "revision", "generation"):
            if key in context and context[key] != cursor.get(key):
                raise ValueError("caller_context_conflict:" + key)
        if "phase" in context and context["phase"] != phase:
            raise ValueError("caller_context_conflict:phase")
        core_phase = cursor.get("phase")
        if phase == "prove":
            phase = "proof"
        if core_phase and core_phase not in ("unplanned", "observe", phase):
            # A pure observation can accompany an active task at any phase.
            if not (payload.get("phase") == "observe" and all(isinstance(n, dict) and n.get("kind") == "observe" for n in payload.get("nodes", []))):
                raise ValueError("core_phase_conflict")
    nodes = payload.get("nodes")
    if not isinstance(nodes, list) or not nodes or len(nodes) > MAX_WORK:
        raise ValueError("invalid_nodes")
    by_id = {}
    for n in nodes:
        if (not isinstance(n, dict) or not isinstance(n.get("id"), str) or
            not ID.fullmatch(n["id"]) or n["id"] == "abstain" or n["id"] in by_id or
            n.get("kind") not in KINDS or not isinstance(n.get("action"), dict)):
            raise ValueError("invalid_node")
        deps, resources = n.get("depends_on", []), n.get("resources", [])
        if not isinstance(deps, list) or any(not isinstance(x, str) for x in deps):
            raise ValueError("invalid_dependencies")
        if not isinstance(resources, list) or any(not isinstance(x, str) or not x for x in resources):
            raise ValueError("invalid_resources")
        if "inputs" in n and not isinstance(n["inputs"], dict): raise ValueError("node_inputs_must_be_object")
        group = n.get("choice_group")
        if group is not None and (not isinstance(group, str) or not ID.fullmatch(group) or group == "abstain"):
            raise ValueError("invalid_choice_group")
        if "semantic_review" in n and type(n["semantic_review"]) is not bool:
            raise ValueError("invalid_semantic_review")
        if payload.get("phase") == "build" and n.get("kind") == "check" and n["action"].get("check_kind") not in {"smoke", "safety"}:
            raise ValueError("construction_check_kind_required")
        expectation = n.get("expect")
        if expectation is not None:
            if not isinstance(expectation, dict) or set(expectation) not in ({"path", "equals"}, {"exit_code"}):
                raise ValueError("invalid_postcondition")
            if "exit_code" in expectation and type(expectation["exit_code"]) is not int:
                raise ValueError("invalid_postcondition")
            if "path" in expectation and (not isinstance(expectation["path"], list) or
                 any(type(x) not in (str, int) or (type(x) is int and x < 0) for x in expectation["path"])):
                raise ValueError("invalid_postcondition")
        by_id[n["id"]] = n
    visiting, visited = set(), set()
    def visit(node_id):
        if node_id in visiting: raise ValueError("cyclic_dependencies")
        if node_id in visited: return
        visiting.add(node_id)
        for dep in by_id[node_id].get("depends_on", []):
            if dep not in by_id: raise ValueError("missing_dependency")
            visit(dep)
        visiting.remove(node_id); visited.add(node_id)
    for node_id in by_id: visit(node_id)
    limits = dict(DEFAULTS); limits.update(payload.get("limits") or {})
    for key, low, high in (("max_parallel", 1, 16), ("max_rounds", 1, 64),
                           ("max_executions", 1, 128), ("timeout_seconds", 1, 300)):
        if type(limits[key]) is not int or not low <= limits[key] <= high:
            raise ValueError("invalid_limit:" + key)
    if type(limits["max_model_calls"]) is not int or not 0 <= limits["max_model_calls"] <= 32:
        raise ValueError("invalid_limit:max_model_calls")
    return by_id, limits


def _path(cwd, value):
    if not isinstance(value, str) or not value:
        raise ValueError("path_required")
    return (Path(cwd) / value).resolve()


def validate_action(node, cwd):
    action, kind = node["action"], node["kind"]
    op = action.get("op")
    if kind == "check" and node.get("action", {}).get("check_kind") not in (None, "smoke", "safety"):
        raise ValueError("invalid_check_kind")
    if op in {"read", "stat"}:
        if kind not in {"observe", "check"}: raise ValueError("file_observation_kind")
        _path(action.get("cwd", cwd), action.get("path"))
        if op == "read" and (type(action.get("max_bytes", MAX_OUTPUT)) is not int or
                             not 0 < action.get("max_bytes", MAX_OUTPUT) <= MAX_OUTPUT):
            raise ValueError("read_limit")
    elif op == "argv":
        argv = action.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x for x in argv):
            raise ValueError("argv_required")
        if action.get("check_kind") not in (None, "smoke", "safety"):
            raise ValueError("invalid_check_kind")
        if kind == "observe": raise ValueError("arbitrary_argv_is_not_observation")
        if action.get("cwd") is not None and not isinstance(action["cwd"], str):
            raise ValueError("invalid_cwd")
        if isinstance(action.get("stdin"), (bool, int, float)):
            raise ValueError("invalid_stdin")
    else:
        raise ValueError("unsupported_operation:" + str(op))


@contextlib.contextmanager
def resource_leases(names, directory, timeout):
    """Cross-process leases; brief SQLite transactions never span external work."""
    resources = sorted(set(names)) or []
    owner = uuid.uuid4().hex
    deadline = time.monotonic() + timeout
    dbpath = Path(directory) / "state.sqlite"
    con = sqlite3.connect(dbpath, timeout=5)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS backend_resource_leases (resource TEXT NOT NULL, owner TEXT NOT NULL, expires REAL NOT NULL, PRIMARY KEY(resource,owner))")
        con.commit()
        while True:
            try:
                con.execute("BEGIN IMMEDIATE")
                now = time.time()
                con.execute("DELETE FROM backend_resource_leases WHERE expires<?", (now,))
                wanted = resources or []
                conflict = False
                for name in wanted:
                    if name == "*":
                        conflict |= con.execute("SELECT 1 FROM backend_resource_leases WHERE owner<>? LIMIT 1", (owner,)).fetchone() is not None
                    else:
                        conflict |= con.execute("SELECT 1 FROM backend_resource_leases WHERE owner<>? AND resource IN (?, '*') LIMIT 1", (owner, name)).fetchone() is not None
                if not conflict:
                    expires = now + max(1.0, timeout + 5.0)
                    con.executemany("INSERT INTO backend_resource_leases(resource,owner,expires) VALUES(?,?,?)",
                                    [(name, owner, expires) for name in wanted])
                    con.commit(); break
                con.rollback()
            except sqlite3.OperationalError:
                con.rollback()
            if time.monotonic() >= deadline:
                raise TimeoutError("resource_lease_pending")
            time.sleep(.05)
        yield
    finally:
        try:
            with con:
                con.execute("DELETE FROM backend_resource_leases WHERE owner=?", (owner,))
        finally:
            con.close()


def execute(node, remaining, cwd, data_dir):
    action = node["action"]; here = str(Path(action.get("cwd", cwd)).resolve())
    op = action["op"]
    if op == "read":
        path = _path(here, action["path"])
        if not path.is_file(): return {"exit_code": 1, "result": {"path": str(path), "exists": False}}
        limit = action.get("max_bytes", MAX_OUTPUT)
        with path.open("rb") as f: blob = f.read(limit + 1)
        return {"exit_code": 0, "result": {"path": str(path), "text": blob[:limit].decode("utf-8", "replace"),
                "bytes": path.stat().st_size, "truncated": len(blob) > limit}}
    if op == "stat":
        path = _path(here, action["path"])
        try:
            st = path.stat(); exists = path.is_file()
            return {"exit_code": 0 if exists else 1, "result": {"path": str(path), "exists": exists,
                    "size": st.st_size, "mtime_ns": st.st_mtime_ns}}
        except FileNotFoundError:
            return {"exit_code": 0, "result": {"path": str(path), "exists": False, "size": 0, "mtime_ns": 0}}
    payload = action.get("stdin")
    if isinstance(payload, (dict, list)): payload = dump(payload)
    if payload is not None and not isinstance(payload, str): raise ValueError("invalid_stdin")
    env = {k: v for k, v in os.environ.items() if k in CONTROL_ENV}
    try:
        p = subprocess.run(action["argv"], cwd=here, env=env, input=payload, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=max(.001, remaining),
                           shell=False, check=False)
        raw = {"exit_code": p.returncode, "stdout": p.stdout[:MAX_OUTPUT], "stderr": p.stderr[:MAX_OUTPUT]}
        try: raw["result"] = json.loads(p.stdout[:MAX_OUTPUT])
        except (ValueError, TypeError): pass
        return raw
    except subprocess.TimeoutExpired:
        return {"exit_code": 124, "stderr": "work operation timed out"}


def postcondition(node, result):
    expect = node.get("expect")
    if expect is None: return None
    if "exit_code" in expect: return result.get("exit_code") == expect["exit_code"]
    value = result
    for key in expect["path"]:
        if isinstance(value, dict) and key in value: value = value[key]
        elif isinstance(value, list) and type(key) is int and 0 <= key < len(value): value = value[key]
        else: return False
    return value == expect["equals"]


def config_path(data_dir):
    return Path(data_dir) / "backend_config.json"


def load_backend_config(data_dir):
    cfg = {"cloud_enabled": False, "mode": "observe", "credential_runner": [],
           "constraints": [], "max_calls_per_turn": 32}
    path = config_path(data_dir)
    if path.is_file():
        raw = path.read_bytes()
        if len(raw) > 16384: raise ValueError("config_bound")
        given = json.loads(raw)
        if not isinstance(given, dict) or set(given) - set(cfg): raise ValueError("invalid_config_keys")
        cfg.update(given)
    if type(cfg["cloud_enabled"]) is not bool or cfg["mode"] not in ("observe", "guard"):
        raise ValueError("invalid_config")
    for key in ("credential_runner", "constraints"):
        value = cfg[key]
        if not isinstance(value, list) or not all(isinstance(x, str) and x and "\0" not in x for x in value):
            raise ValueError("invalid_config_list")
    if len(cfg["credential_runner"]) > 16 or len(dump(cfg["constraints"])) > 6000:
        raise ValueError("config_bound")
    if type(cfg["max_calls_per_turn"]) is not int or not 1 <= cfg["max_calls_per_turn"] <= 256:
        raise ValueError("invalid_call_budget")
    return cfg


def save_backend_config(data_dir, cfg):
    path = config_path(data_dir); private_dir(path.parent)
    fd, temp = tempfile.mkstemp(prefix=".backend-config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dump(cfg) + "\n"); f.flush(); os.fsync(f.fileno())
        os.chmod(temp, 0o600); os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def provider_transport(payload, *, cfg, data_dir):
    """Direct TypeSafe transport for an already-authorized foreground envstack."""
    if not cfg.get("cloud_enabled"):
        raise RuntimeError("cloud_disabled")
    if os.environ.get("TYPESAFE_API_KEY"):
        semantics, _ = load_semantics()
        return semantics.request_api(payload["state"], payload["questions"])
    raise RuntimeError("provider_unavailable")


def provider_operation(op, payload, data_dir, cfg):
    """Run the whole typed operation in one explicit foreground credential runner."""
    runner = cfg.get("credential_runner") or []
    if not runner: raise RuntimeError("provider_unavailable")
    command = list(runner) + [sys.executable, str(Path(__file__).resolve()), "_provider",
                              "--operation", op, "--data-dir", str(data_dir)]
    env = {k: v for k, v in os.environ.items() if k in CONTROL_ENV}
    try:
        proc = subprocess.run(command, input=dump(payload), text=True, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=40, shell=False, check=False)
        if proc.returncode or len(proc.stdout.encode()) > 32768:
            raise RuntimeError("credential_runner_failed")
        value = json.loads(proc.stdout)
        if not isinstance(value, dict): raise ValueError("provider_response")
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("credential_runner_unavailable") from exc


def typed_evaluate(payload, op, data_dir, cfg):
    semantics, selection = load_semantics()
    if semantics.redact(payload) != payload:
        return {"status": "unassessed", "reason_codes": ["redacted_input"], "answers": {},
                "usage": {"input_tokens": 0, "output_tokens": 0}, "evaluations": 0, "cache_hit": False}
    if cfg["cloud_enabled"] and not os.environ.get("TYPESAFE_API_KEY") and cfg.get("credential_runner"):
        try: return provider_operation(op, payload, data_dir, cfg)
        except RuntimeError:
            return {"status": "unassessed", "reason_codes": ["credential_runner_unavailable"], "answers": {},
                    "usage": {"input_tokens": 0, "output_tokens": 0}, "evaluations": 0, "cache_hit": False}
    def evaluate(req):
        if not cfg["cloud_enabled"]:
            return {"status": "unassessed", "reason_codes": ["cloud_disabled"], "answers": {},
                    "usage": {"input_tokens": 0, "output_tokens": 0}, "evaluations": 0, "cache_hit": False}
        return semantics.decisions.evaluate(
            req,
            request=lambda state, questions: provider_transport({"state": state, "questions": questions}, cfg=cfg, data_dir=data_dir),
            redact=semantics.redact, directory=data_dir, enabled=True,
            model=semantics.MODEL, engine_sha256=semantics.ENGINE_SHA256)
    if op == "decide": return evaluate(payload)
    if op == "select": return selection.select(payload, evaluate=evaluate)
    raise ValueError("invalid_typed_operation")


def assess_state(state, mode, data_dir, cfg):
    semantics, _ = load_semantics()
    if not cfg["cloud_enabled"]:
        return semantics.verdict("unassessed", "cloud_disabled", state)
    if not os.environ.get("TYPESAFE_API_KEY") and cfg.get("credential_runner"):
        try: return provider_operation("verify" if mode == "verify" else "assess", state, data_dir, cfg)
        except RuntimeError: return semantics.verdict("unassessed", "credential_runner_unavailable", state)
    transport = lambda payload: provider_transport(payload, cfg=cfg, data_dir=data_dir)
    return semantics.evaluate(state, cfg, transport=transport, mode=mode)


def semantic_wave(candidates, payload, receipts, generation, data_dir, cfg):
    questions, groups = {}, {}
    for node in candidates:
        ident = node["id"]
        if node["kind"] == "decision":
            supplied = node["action"].get("questions")
            if not isinstance(supplied, dict): raise ValueError("decision_questions_required")
            for qid, question in supplied.items(): questions[f"decision/{ident}/{qid}"] = question
        else:
            questions[f"scope/{ident}"] = {"type": "noul", "instructions":
                "Does the actual action conflict with the pinned context.request and supplied constraints? Assess the action itself, not its description."}
        group = node.get("choice_group")
        if group: groups.setdefault(group, []).append(node)
    for group, options in groups.items():
        criteria = {n["id"]: n["action"] for n in options}; criteria["abstain"] = "No suitable supplied action"
        questions[f"choice/{group}"] = {"type": "choice", "instructions":
            "Select one supplied action that meets the pinned request and constraints; abstain if uncertain.", "criteria": criteria}
    if not questions: return candidates, {}, [], None, 0
    if len(questions) > 32: return [], {}, [], "decision_batch_limit", 0
    state = {"request": payload["context"]["request"],
             "constraints": payload["context"].get("constraints", []),
             "actions": {n["id"]: n["action"] for n in candidates},
             "observations": receipts, "generation": generation}
    cache = {"namespace": digest({"session": generation.get("session_id"), "work": payload["id"]}),
             "generation": digest(generation)}
    result = typed_evaluate({"state": state, "questions": questions, "cache": cache}, "decide", data_dir, cfg)
    if result.get("status") != "ok" or set(result.get("answers", {})) != set(questions):
        return [], {}, [], "typed_decision_unassessed", result.get("evaluations", 0)
    allowed, skipped, decisions = [], [], {}
    selected = {}
    for group in groups:
        answer = result["answers"][f"choice/{group}"]
        if answer["confidence"] < .8 or answer["choice"] == "abstain": return [], {}, [], "choice_needs_primary", result.get("evaluations", 0)
        selected[group] = answer["choice"]
    for node in candidates:
        ident = node["id"]
        if node["kind"] == "decision":
            prefix = f"decision/{ident}/"
            decisions[ident] = {"status": "decided", "verified": False,
                                "answers": {k[len(prefix):]: v for k, v in result["answers"].items() if k.startswith(prefix)}}
        elif node.get("choice_group") and selected[node["choice_group"]] != ident:
            skipped.append(ident)
        else:
            conflict = result["answers"][f"scope/{ident}"]["noul"]
            if conflict >= .9: return [], {}, [], "source_conflict_blocked", result.get("evaluations", 0)
            if conflict >= .4: return [], {}, [], "scope_needs_primary", result.get("evaluations", 0)
            allowed.append(node)
    return allowed, decisions, skipped, None, result.get("evaluations", 0)


def run_work(payload, cursor, raw_input, data_dir):
    semantics, _ = load_semantics()
    if semantics.redact(payload) != payload: raise ValueError("redacted_input_refused")
    nodes, limits = validate(payload, cursor)
    context = payload["context"]
    cwd = str(Path(context.get("cwd") or os.getcwd()).resolve())
    for node in nodes.values():
        if node["kind"] == "decision":
            semantics.decisions._questions(node["action"].get("questions"))
            continue
        validate_action(node, cwd)
        action = node["action"]
        if action.get("op") in {"read", "stat"}:
            path = _path(action.get("cwd", cwd), action["path"])
            try:
                st = path.stat()
                current = {"path": str(path), "exists": True, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
            except FileNotFoundError:
                current = {"path": str(path), "exists": False}
            node["inputs"] = {**node.get("inputs", {}), "file": current}
        elif "inputs" in node and not isinstance(node["inputs"], dict):
            raise ValueError("node_inputs_must_be_object")
    generation = {k: cursor.get(k) for k in ("session_id", "revision", "generation")} if cursor else {
        "session_id": os.environ.get("CTRLISSUES_SESSION"), "revision": None, "generation": None}
    cfg = load_backend_config(data_dir) if any(n["kind"] in {"action", "check", "decision"} for n in nodes.values()) else None
    has_effect = any(n["kind"] == "action" for n in nodes.values())
    task_session = (generation.get("session_id") or context.get("session_id") or
                    context.get("task_id") or os.environ.get("CTRLISSUES_SESSION"))
    if has_effect and not task_session:
        raise ValueError("action_task_session_required")
    if generation.get("session_id") is None:
        generation["session_id"] = task_session
    freshness_source = {"session_id": task_session, "generation": generation.get("generation")}
    input_hash = digest({"input": payload, "source": freshness_source})
    work_identity = {"session_id": task_session, "work_id": payload["id"]}
    key = digest(work_identity if has_effect else {**work_identity, "input": input_hash})
    dbpath = Path(data_dir) / "state.sqlite"
    private_dir(dbpath.parent)
    new_database = not dbpath.exists()
    con = sqlite3.connect(dbpath, timeout=5)
    if new_database:
        try: os.chmod(dbpath, 0o600)
        except OSError: pass
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("CREATE TABLE IF NOT EXISTS backend_work_runs (k TEXT PRIMARY KEY, input_hash TEXT NOT NULL, status TEXT NOT NULL, report TEXT NOT NULL, created REAL NOT NULL)")
        old = con.execute("SELECT input_hash,status,report FROM backend_work_runs WHERE k=?", (key,)).fetchone()
        if old:
            con.rollback()
            if old[0] != input_hash:
                return {"status": "unknown" if has_effect else "pending", "reason": "work_input_changed_no_action_retry",
                        "executed": 0, "verified": 0, "reused": 0, "failures": []}
            if old[1] == "complete":
                result = json.loads(old[2]); result["replayed"] = True
                result["executed"] = 0; result["reused"] = len(result.get("receipts", {}))
                return result
            return {"status": "pending" if old[1] == "running" else old[1], "reason": "prior_run_not_complete_no_retry",
                    "executed": 0, "verified": 0, "reused": 0, "failures": []}
        con.execute("INSERT INTO backend_work_runs VALUES(?,?,?,?,?)", (key, input_hash, "running", "{}", time.time()))
        con.commit()
    finally: con.close()

    receipts, failures, executed, reused, reason = {}, [], 0, 0, None
    skipped = []
    start = time.monotonic(); deadline = start + limits["timeout_seconds"]
    model_calls = 0
    remaining_nodes = set(nodes)
    for _round in range(limits["max_rounds"]):
        if not remaining_nodes: break
        ready = [nodes[i] for i in nodes if i in remaining_nodes and all(d in receipts for d in nodes[i].get("depends_on", []))]
        if not ready: reason = "pending_dependencies"; break
        semantic_nodes = [n for n in ready if n["kind"] == "decision" or n.get("choice_group") or n.get("semantic_review") is True]
        candidates = [n for n in ready if n not in semantic_nodes]
        deferred_reason = None
        if semantic_nodes:
            if model_calls >= limits["max_model_calls"]:
                deferred_reason = "model_call_limit"
            else:
                allowed, decisions, excluded, decision_error, call_count = semantic_wave(
                    semantic_nodes, payload, receipts, generation, data_dir, cfg)
                model_calls += call_count
                receipts.update(decisions)
                for ident in decisions: remaining_nodes.remove(ident)
                for ident in excluded:
                    skipped.append(ident); remaining_nodes.remove(ident)
                candidates.extend(allowed)
                if decision_error:
                    deferred_reason = decision_error
        if not candidates:
            if deferred_reason:
                reason = deferred_reason; break
            continue
        parallel = []; occupied = set()
        for node in candidates:
            resources = set(node.get("resources", []))
            if node["kind"] != "observe" and not resources: resources = {"*"}
            if len(parallel) >= limits["max_parallel"] or "*" in resources and parallel or "*" in occupied or resources & occupied: continue
            parallel.append(node); occupied.update(resources)
        if not parallel: reason = "resource_schedule_pending"; break
        if executed + len(parallel) > limits["max_executions"]:
            parallel = parallel[:max(0, limits["max_executions"]-executed)]
        if not parallel: reason = "execution_limit"; break
        def run_node(node):
            ident = node["id"]; resources = set(node.get("resources", []))
            if node["kind"] != "observe" and not resources: resources = {"*"}
            try:
                left = deadline-time.monotonic()
                if left <= 0: raise TimeoutError("deadline")
                with resource_leases(resources, Path(data_dir), left):
                    result = execute(node, deadline-time.monotonic(), cwd, data_dir)
                    result = semantics.redact(result)
                expected = postcondition(node, result)
                command_ok = result.get("exit_code") == 0
                success = expected is True if node.get("expect") is not None else command_ok
                status = "failed" if not success else "verified" if expected is True else "executed"
                receipt = {"status": status, "verified": expected is True, "action": node["action"],
                           "postcondition": node.get("expect"), "result": result,
                           "source_generation": generation, "input_sha256": input_hash}
                return ident, receipt, True
            except TimeoutError as exc:
                return ident, {"status": "pending", "verified": False, "reason": str(exc),
                               "source_generation": generation, "input_sha256": input_hash}, False
            except Exception as exc:
                return ident, {"status": "failed", "verified": False, "reason": type(exc).__name__,
                               "source_generation": generation, "input_sha256": input_hash}, False
        with ThreadPoolExecutor(max_workers=min(len(parallel), limits["max_parallel"])) as pool:
            futures = [pool.submit(run_node, node) for node in parallel]
            for future in as_completed(futures):
                ident, receipt, invoked = future.result()
                executed += int(invoked)
                receipts[ident] = receipt; remaining_nodes.remove(ident)
                if receipt["status"] in {"failed", "pending"}:
                    failures.append({"id": ident, "status": receipt["status"], "fact": receipt.get("reason", "postcondition_or_exit_failed")})
        if deferred_reason:
            reason = deferred_reason
            break
        if reason: break
        if failures: break
    pending = sorted(remaining_nodes)
    status = ("failed" if failures else "blocked" if reason == "source_conflict_blocked" else
              "unknown" if reason == "typed_decision_unassessed" else "pending" if pending or reason else "complete")
    verified_count = sum(1 for r in receipts.values() if r.get("verified"))
    result = {"schema": "ctrlissues.work-report.v1", "status": status,
              "work_id": payload["id"], "source_generation": generation,
              "input_sha256": input_hash, "receipts": receipts,
              "pending": pending, "skipped": sorted(skipped), "failures": failures, "reason": reason,
              "executed": executed, "verified": verified_count, "reused": reused, "model_calls": model_calls,
              "elapsed_ms": round((time.monotonic()-start)*1000, 2)}
    con = sqlite3.connect(dbpath, timeout=5)
    try:
        con.execute("UPDATE backend_work_runs SET status=?,report=? WHERE k=?", (status, dump(result), key)); con.commit()
    finally: con.close()
    return result


def write_report(path, result):
    target = Path(path).expanduser().resolve(); private_dir(target.parent)
    fd, temp = tempfile.mkstemp(prefix=".ctrlissues-report-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dump(result)); f.flush(); os.fsync(f.fileno())
        os.chmod(temp, 0o600); os.replace(temp, target)
    finally:
        if os.path.exists(temp): os.unlink(temp)
    return str(target)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ctrlissues-backend")
    parser.add_argument("--data-dir", default=os.environ.get("CTRLISSUES_DATA"))
    subs = parser.add_subparsers(dest="command", required=True)
    work = subs.add_parser("work"); work.add_argument("--input", required=True); work.add_argument("--report", required=True)
    for op in ("decide", "select", "assess", "verify", "guard"):
        sub = subs.add_parser(op); sub.add_argument("--input", required=True)
    subs.add_parser("stats")
    config = subs.add_parser("configure")
    config.add_argument("--cloud", choices=("on", "off"))
    config.add_argument("--mode", choices=("observe", "guard"))
    config.add_argument("--credential-runner-json")
    config.add_argument("--constraints-json")
    child = subs.add_parser("_provider")
    child.add_argument("--data-dir", dest="provider_data", required=True)
    child.add_argument("--operation", choices=("decide", "select", "assess", "verify"), required=True)
    args = parser.parse_args(argv)
    try:
        data_dir = args.provider_data if args.command == "_provider" else args.data_dir
        if not data_dir: raise ValueError("CTRLISSUES_DATA_required")
        data_dir = str(Path(data_dir).expanduser().resolve())
        if args.command == "_provider":
            payload = json.load(sys.stdin)
            semantics, selection = load_semantics()
            cfg = load_backend_config(data_dir)
            if args.operation in {"decide", "select"}:
                if not isinstance(payload, dict): raise ValueError("provider_payload")
                def evaluate(req):
                    return semantics.decisions.evaluate(
                        req,
                        request=lambda state, questions: semantics.request_api(state, questions),
                        redact=semantics.redact, directory=data_dir, enabled=True,
                        model=semantics.MODEL, engine_sha256=semantics.ENGINE_SHA256)
                out = evaluate(payload) if args.operation == "decide" else selection.select(payload, evaluate=evaluate)
            else:
                out = semantics.evaluate(payload, cfg, mode="verify" if args.operation == "verify" else "check")
            print(dump(out)); return 0
        if args.command == "configure":
            cfg = load_backend_config(data_dir)
            if args.cloud is not None: cfg["cloud_enabled"] = args.cloud == "on"
            if args.mode is not None: cfg["mode"] = args.mode
            if args.credential_runner_json is not None:
                cfg["credential_runner"] = json.loads(args.credential_runner_json)
            if args.constraints_json is not None:
                cfg["constraints"] = json.loads(args.constraints_json)
            # Run the full validation before persisting user-supplied settings.
            temp = Path(data_dir) / ".backend-config-validation.json"
            if not isinstance(cfg["credential_runner"], list) or not all(isinstance(x, str) and x and "\0" not in x for x in cfg["credential_runner"]):
                raise ValueError("invalid_credential_runner")
            if not isinstance(cfg["constraints"], list) or not all(isinstance(x, str) and x and "\0" not in x for x in cfg["constraints"]):
                raise ValueError("invalid_constraints")
            if len(cfg["credential_runner"]) > 16 or len(dump(cfg)) > 16000: raise ValueError("config_bound")
            semantics, _ = load_semantics()
            if semantics.redact(cfg) != cfg: raise ValueError("redacted_config_refused")
            save_backend_config(data_dir, cfg)
            print(dump({"status": "configured", "cloud_enabled": cfg["cloud_enabled"], "mode": cfg["mode"],
                        "credential_runner_configured": bool(cfg["credential_runner"])})); return 0
        if args.command == "stats":
            semantics, _ = load_semantics()
            report = semantics.decisions.stats(data_dir)
            con = sqlite3.connect(Path(data_dir) / "state.sqlite", timeout=5)
            try:
                row = con.execute("SELECT COUNT(*),SUM(status='complete'),SUM(status='failed') FROM backend_work_runs").fetchone()
                report["work_runs"], report["work_complete"], report["work_failed"] = (row if row else (0, 0, 0))
            except sqlite3.Error:
                report.update(work_runs=0, work_complete=0, work_failed=0)
            finally: con.close()
            print(dump({"status": "ok", "stats": report})); return 0
        if args.command in {"decide", "select"}:
            payload, _ = load_input(args.input)
            result = typed_evaluate(payload, args.command, data_dir, load_backend_config(data_dir))
            print(dump(result)); return 0 if result.get("status") in {"ok", "partial"} else 4
        if args.command in {"assess", "verify", "guard"}:
            payload, _ = load_input(args.input)
            result = assess_state(payload, "verify" if args.command == "verify" else "check", data_dir, load_backend_config(data_dir))
            print(dump(result)); return {"pass": 0, "review": 2, "block": 3, "unassessed": 4}[result["decision"]]
        payload, raw = load_input(args.input)
        cursor, cursor_error = core_status()
        if cursor_error and os.environ.get("CTRLISSUES_SESSION"):
            result = {"schema": "ctrlissues.work-report.v1", "status": "unknown", "reason": cursor_error,
                      "executed": 0, "verified": 0, "reused": 0, "failures": []}
        else:
            result = run_work(payload, cursor, raw, data_dir)
        locator = write_report(args.report, result)
        compact = {"status": result.get("status", "unknown"), "executed": result.get("executed", 0),
                   "verified": result.get("verified", 0), "reused": result.get("reused", 0),
                   "failed": result.get("failures", []), "report": locator}
        print(dump(compact)); return 0 if result.get("status") == "complete" else 1
    except Exception as exc:
        print(dump({"status": "failed", "reason": str(exc), "executed": 0,
                    "verified": 0, "reused": 0, "failed": []}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
