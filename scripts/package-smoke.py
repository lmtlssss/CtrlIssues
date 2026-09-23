#!/usr/bin/env python3
"""Smoke-test an immutable package through isolated install, launch, and rollback."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def generation(hashes: dict[str, str]) -> str:
    value = hashlib.sha256()
    for name, digest in sorted(hashes.items()):
        value.update(name.encode() + b"\0" + bytes.fromhex(digest))
    return value.hexdigest()


def package_variant(source: Path, destination: Path) -> str:
    with zipfile.ZipFile(source) as archive:
        metadata = json.loads(archive.read("ctrlissues-package.json"))
        files = {name: archive.read(name) for name in archive.namelist() if name != "ctrlissues-package.json"}
    marker = "plugins/ctrlissues/hands/UPSTREAM.md"
    if marker not in files:
        raise RuntimeError("package is missing the CtrlIssues hands reference")
    files[marker] += b"\nPackage rollback smoke generation.\n"
    hashes = {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}
    metadata["files"] = hashes
    metadata["generation"] = generation(hashes)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, body in sorted(files.items()):
            archive.writestr(name, body)
        archive.writestr("ctrlissues-package.json", json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n")
    return metadata["generation"]


def package_metadata(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        return json.loads(archive.read("ctrlissues-package.json"))


def ordinary_environment(temp: Path) -> dict[str, str]:
    allowed = {
        "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
        "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE", "APPDATA", "LOCALAPPDATA",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    home = temp / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["TMPDIR"] = env["TMP"] = env["TEMP"] = str(temp)
    return env


def run_checked(command: list[str], env: dict[str, str], *, label: str) -> str:
    result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError(f"{label} failed ({result.returncode}): {(result.stderr or result.stdout)[-2000:]}")
    return result.stdout


def install(installer: Path, package: Path, codex: Path, data: Path, bindir: Path, env: dict[str, str]) -> None:
    run_checked([
        sys.executable, str(installer), "--package", str(package), "--codex-root", str(codex),
        "--prefix", str(data), "--bin-dir", str(bindir), "--no-activate",
    ], env, label="isolated package install")


def launch(launcher: Path, env: dict[str, str], *args: str) -> dict:
    launch_env = dict(env)
    launch_env["CODEX_THREAD_ID"] = "ci-package-smoke"
    if os.name == "nt":
        command = [launch_env.get("COMSPEC", "cmd.exe"), "/d", "/c", "call", str(launcher), *args]
    else:
        command = [str(launcher), *args]
    output = run_checked(command, launch_env, label="installed launcher")
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError("installed launcher did not return JSON status") from error


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--checksum", type=Path, required=True)
    args = parser.parse_args(argv)
    package = args.package.resolve()
    expected = hashlib.sha256(package.read_bytes()).hexdigest().encode() + b"  " + package.name.encode() + b"\n"
    if args.checksum.read_bytes() != expected:
        raise RuntimeError("package checksum file is not the exact lowercase SHA-256 line")

    root = Path(__file__).resolve().parents[1]
    installer = root / "scripts/install.py"
    with tempfile.TemporaryDirectory(prefix="ctrlissues-package-smoke-") as temporary:
        temp = Path(temporary)
        env = ordinary_environment(temp)
        codex = temp / "codex-home"
        data = temp / "isolated-data"
        bindir = temp / "bin"
        variant = temp / "variant.zip"
        first_generation = package_variant(package, variant)

        install(installer, package, codex, data, bindir, env)
        current = (data / "current").read_text(encoding="ascii").strip()
        first_metadata = package_metadata(package)
        if current != "payloads/" + first_metadata["generation"]:
            raise RuntimeError("first package pointer does not name its immutable generation")
        first_payload = data / current
        native_name = "ctrlissues.exe" if os.name == "nt" else "ctrlissues"
        if not (first_payload / "plugins/ctrlissues/runtime/bin" / native_name).is_file():
            raise RuntimeError("installed generation does not have the documented native binary layout")
        launcher = bindir / ("ctrlissues.cmd" if os.name == "nt" else "ctrlissues")
        first_status = launch(launcher, env, "status", "--compact")
        state = data / "state.sqlite"
        if first_status.get("schema") != "ctrlissues.cursor.v1" or first_status.get("session_id") != "ci-package-smoke":
            raise RuntimeError("installed launcher did not return the native compact status")
        backend_status = launch(launcher, env, "stats")
        if backend_status.get("status") != "ok":
            raise RuntimeError("installed package did not launch its Python backend")
        if not state.is_file() or state.stat().st_size == 0:
            raise RuntimeError("installed native launcher did not create isolated state")

        install(installer, variant, codex, data, bindir, env)
        second_pointer = (data / "current").read_text(encoding="ascii").strip()
        if second_pointer != "payloads/" + first_generation:
            raise RuntimeError("second package did not select the new generation")
        previous = (data / "previous").read_text(encoding="ascii").strip()
        if previous != "payloads/" + first_metadata["generation"]:
            raise RuntimeError("installer did not retain the prior generation")
        launch(launcher, env, "status", "--compact")
        if not state.is_file():
            raise RuntimeError("package replacement removed the existing state database")

        rollback = run_checked([
            sys.executable, str(installer), "--rollback", "--codex-root", str(codex), "--prefix", str(data),
        ], env, label="isolated package rollback")
        json.loads(rollback)
        if (data / "current").read_text(encoding="ascii").strip() != previous:
            raise RuntimeError("rollback did not restore the prior generation")
        if not state.is_file() or not launch(launcher, env, "status", "--compact"):
            raise RuntimeError("rolled-back native launcher did not retain and open package state")

        print(json.dumps({"status": "valid", "first_generation": first_metadata["generation"],
                          "variant_generation": first_generation, "rollback": "verified"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, zipfile.BadZipFile) as error:
        print("CtrlIssues package smoke stopped: " + str(error), file=sys.stderr)
        raise SystemExit(1)
