#!/usr/bin/env python3
"""Build one immutable CtrlIssues source, hook, skill, backend, and binary package."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import zipfile

FORMAT = "ctrlissues.package.v1"
MAX_FILE = 64 * 1024 * 1024
EXTRA_FILES = ("LICENSE", "NOTICE.md", "README.md", "install.sh", "install.ps1", "scripts/install.py", "scripts/package.py",
               "scripts/validate-package.py", "scripts/prove-system.sh", "scripts/prove-system.ps1", "docs/install.md")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest_inventory(files):
    h = hashlib.sha256()
    for name in sorted(files):
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ValueError("unsafe_inventory_path")
        body = files[name]
        if len(body) > MAX_FILE: raise ValueError("file_bound:" + name)
        h.update(name.encode() + b"\0" + hashlib.sha256(body).digest())
    return h.hexdigest()


def collect(root: Path, binary: Path, target: str) -> dict[str, bytes]:
    root = root.resolve(); plugin = root / "plugins/ctrlissues"
    if not plugin.is_dir(): raise ValueError("plugin_root_missing")
    files = {}
    marketplace = root / ".agents/plugins/marketplace.json"
    if not marketplace.is_file(): raise ValueError("repo_marketplace_missing")
    files[".agents/plugins/marketplace.json"] = marketplace.read_bytes()
    for path in plugin.rglob("*"):
        if path.is_dir(): continue
        relative = path.relative_to(root).as_posix()
        if any(part in {"target", "__pycache__", ".pytest_cache"} for part in path.relative_to(plugin).parts): continue
        if path.is_symlink() or not path.is_file(): raise ValueError("unsafe_source_file:" + relative)
        files[relative] = path.read_bytes()
    for name in EXTRA_FILES:
        path = root / name
        if not path.is_file() or path.is_symlink(): raise ValueError("package_file_missing:" + name)
        files[name] = path.read_bytes()
    info = json.loads(files["plugins/ctrlissues/.codex-plugin/plugin.json"])
    if info.get("name") != "ctrlissues" or info.get("version") != "0.1.0": raise ValueError("plugin_manifest_mismatch")
    if not binary.is_file() or binary.is_symlink(): raise ValueError("binary_missing_or_unsafe")
    suffix = ".exe" if target.endswith("windows-msvc") else ""
    binary_name = "plugins/ctrlissues/runtime/bin/ctrlissues" + suffix
    files[binary_name] = binary.read_bytes()
    if not files[binary_name]: raise ValueError("empty_binary")
    return files


def package(root: Path, binary: Path, target: str, output: Path) -> dict:
    files = collect(root, binary, target)
    hashes = {name: hashlib.sha256(body).hexdigest() for name, body in sorted(files.items())}
    generation = digest_inventory(files)
    metadata = {"format": FORMAT, "name": "ctrlissues", "version": "0.1.0",
                "target": target, "generation": generation, "files": hashes}
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name("." + output.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            members = {**files, "ctrlissues-package.json": canonical(metadata) + b"\n"}
            for name, body in sorted(members.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                executable = name.endswith((".sh", "/ctrlissues")) and not name.endswith(".exe")
                info.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, body)
        os.replace(tmp, output)
    finally:
        tmp.unlink(missing_ok=True)
    return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = package(args.source, args.binary, args.target, args.output)
    print(json.dumps({"package": str(args.output.resolve()), "generation": result["generation"],
                      "files": len(result["files"]), "target": result["target"]}, sort_keys=True))


if __name__ == "__main__":
    main()
