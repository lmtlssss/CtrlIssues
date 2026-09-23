#!/usr/bin/env python3
"""Validate a CtrlIssues source tree or immutable package structure and hashes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import zipfile

FORMAT = "ctrlissues.package.v1"
MAX_FILES = 1024
MAX_TOTAL = 96 * 1024 * 1024
TARGETS = {"x86_64-unknown-linux-musl", "aarch64-apple-darwin", "x86_64-apple-darwin", "x86_64-pc-windows-msvc"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def generation(hashes):
    h = hashlib.sha256()
    for name, value in sorted(hashes.items()): h.update(name.encode() + b"\0" + bytes.fromhex(value))
    return h.hexdigest()


def validate_tree(root: Path, require_binary=False):
    root = root.resolve()
    manifest = json.loads((root / "plugins/ctrlissues/.codex-plugin/plugin.json").read_text(encoding="utf-8"))
    market = json.loads((root / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
    hooks = json.loads((root / "plugins/ctrlissues/hooks/hooks.json").read_text(encoding="utf-8"))
    skill = (root / "plugins/ctrlissues/skills/ctrlissues/SKILL.md").read_text(encoding="utf-8")
    entries = [p for p in market.get("plugins", []) if isinstance(p, dict) and p.get("name") == "ctrlissues"]
    if manifest.get("name") != "ctrlissues" or manifest.get("version") != "0.1.1": raise ValueError("plugin_manifest")
    if len(entries) != 1 or entries[0].get("source") != {"source": "local", "path": "./plugins/ctrlissues"}:
        raise ValueError("marketplace_entry")
    if not all(k in entries[0].get("policy", {}) for k in ("installation", "authentication")) or not entries[0].get("category"):
        raise ValueError("marketplace_policy")
    expected_hooks = {"SessionStart", "SubagentStart", "UserPromptSubmit", "PreCompact", "PreToolUse", "PostToolUse"}
    if set(hooks.get("hooks", {})) != expected_hooks: raise ValueError("hook_definitions")
    if "for agents with executive dysfunction." not in manifest.get("description", "") or "for agents with executive dysfunction." not in skill:
        raise ValueError("approved_tagline")
    for ref in ("references/task.md", "references/execution.md"):
        if not (root / "plugins/ctrlissues/skills/ctrlissues" / ref).is_file(): raise ValueError("skill_reference:" + ref)
    if require_binary:
        candidates = list((root / "plugins/ctrlissues/runtime/bin").glob("ctrlissues*"))
        if len(candidates) != 1 or not candidates[0].is_file() or candidates[0].stat().st_size == 0: raise ValueError("binary")
    return manifest


def validate_package(path: Path):
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES or sum(i.file_size for i in infos) > MAX_TOTAL: raise ValueError("package_bound")
        names = set()
        for info in infos:
            name = info.filename; posix = PurePosixPath(name)
            if (posix.is_absolute() or ".." in posix.parts or "\\" in name or PureWindowsPath(name).drive
                    or name in names or stat.S_ISLNK(info.external_attr >> 16)):
                raise ValueError("unsafe_archive_path")
            names.add(name)
        if "ctrlissues-package.json" not in names: raise ValueError("package_metadata")
        metadata = json.loads(zf.read("ctrlissues-package.json"))
        if metadata.get("format") != FORMAT or metadata.get("name") != "ctrlissues" or metadata.get("version") != "0.1.1":
            raise ValueError("package_identity")
        if metadata.get("target") not in TARGETS: raise ValueError("package_target")
        files = metadata.get("files")
        if not isinstance(files, dict) or set(files) != names - {"ctrlissues-package.json"}: raise ValueError("package_inventory")
        for name, expected in files.items():
            if hashlib.sha256(zf.read(name)).hexdigest() != expected: raise ValueError("file_hash:" + name)
        if generation(files) != metadata.get("generation"): raise ValueError("generation_hash")
        plugin = json.loads(zf.read("plugins/ctrlissues/.codex-plugin/plugin.json"))
        if plugin.get("name") != "ctrlissues" or plugin.get("version") != metadata["version"]: raise ValueError("manifest_mismatch")
        suffix = ".exe" if "windows-msvc" in metadata.get("target", "") else ""
        binary = "plugins/ctrlissues/runtime/bin/ctrlissues" + suffix
        if binary not in names or not zf.read(binary): raise ValueError("binary_missing")
        hooks = json.loads(zf.read("plugins/ctrlissues/hooks/hooks.json"))
        if set(hooks.get("hooks", {})) != {"SessionStart", "SubagentStart", "UserPromptSubmit", "PreCompact", "PreToolUse", "PostToolUse"}: raise ValueError("hook_definitions")
        return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source", type=Path)
    group.add_argument("--package", type=Path)
    args = parser.parse_args(argv)
    result = validate_tree(args.source) if args.source else validate_package(args.package)
    print(json.dumps({"status": "valid", "name": "ctrlissues", "version": "0.1.1",
                      "generation": result.get("generation")}, sort_keys=True))


if __name__ == "__main__": main()
