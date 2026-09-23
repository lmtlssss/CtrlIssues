"""Install pinned optional browser dependencies and apply local fail-closed patches."""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import tempfile
from pathlib import Path

MARKER = "# CtrlIssues: dotenv loading disabled for foreground browser runs."
MODULES = ("admin.py", "helpers.py", "daemon.py")


def clean_environment() -> dict[str, str]:
    allowed = {
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "TMP", "TEMP", "TMPDIR",
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
        "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "DISPLAY",
        "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in allowed or key.upper().startswith("LC_")
    }


def patch_runtime(package_dir: Path) -> None:
    replacements = {}
    for name in MODULES:
        path = package_dir / name
        source = path.read_text(encoding="utf-8")
        if MARKER in source:
            replacements[path] = None
            continue
        needle = "\n_load_env()\n"
        if source.count(needle) != 1:
            raise RuntimeError(f"unexpected pinned Browser Harness source at {name}")
        replacements[path] = source.replace(needle, f"\n{MARKER}\n", 1)
    for path, updated in replacements.items():
        if updated is None:
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(updated)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp, mode)
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--plugin-root", required=True, type=Path)
    args = parser.parse_args()
    data, root = args.data_dir.resolve(), args.plugin_root.resolve()
    environment = clean_environment()
    runtime = data / "hands" / "browser-env"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment["UV_PROJECT_ENVIRONMENT"] = str(runtime)
    project = root / "hands/vendor/jev_ultrafast"
    subprocess.run(
        ["uv", "sync", "--locked", "--no-dev", "--python", "3.12", "--project", str(project)],
        env=environment,
        check=True,
    )
    python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    located = subprocess.run(
        [str(python), "-c", "import importlib.metadata; print(importlib.metadata.distribution('browser-harness').locate_file('browser_harness'))"],
        env=environment,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    patch_runtime(Path(located))
    print(f"CtrlIssues optional browser runtime prepared at {runtime}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
