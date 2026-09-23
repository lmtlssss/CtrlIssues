# Install CtrlIssues

The v0.1.1 package contains the native core, Python backend, hooks, skill, and plugin manifest in one immutable versioned payload. It does not replace the Codex executable or add a model catalog. Python 3.11 or newer is needed for installation and the Python backend; Rust is needed only to build from source.

## Build a package

Build the Rust binary for the target platform, then run:

```sh
python3 scripts/package.py --source . --binary PATH_TO_CTRLISSUES --target TARGET_TRIPLE --output ctrlissues-TARGET.zip
python3 scripts/validate-package.py --package ctrlissues-TARGET.zip
```

Targets are Linux x86_64 musl, macOS arm64, macOS x86_64, and Windows x86_64 MSVC. A target is supported only after its CI build and package validation pass.

## Install from source

Source installation needs Rust and Python 3.11 or newer. Build the native core
for the current machine first:

```sh
cargo build --release --locked --manifest-path plugins/ctrlissues/runtime/Cargo.toml
./install.sh --binary PATH_TO_CTRLISSUES
```

On Windows, build the MSVC executable and pass it to
`install.ps1 --binary PATH_TO_CTRLISSUES.exe`. The installer needs the native Codex CLI to register the
local marketplace and plugin and to trust CtrlIssues hooks using current hashes
from Codex app-server. The default Codex root is `$CODEX_HOME`, or `~/.codex`.
Codex configuration changes are limited to CtrlIssues marketplace, plugin,
and hook entries. The installer also manages its own launchers and private
data; it does not snapshot or restore the whole Codex profile.

On Linux x86_64 and macOS arm64/x86_64, the release bootstrap selects the matching versioned package, downloads that package and its `.sha256` file once, checks the digest, and then runs the bundled installer. For example:

```sh
curl -fsSL https://github.com/lmtlssss/CtrlIssues/releases/download/v0.1.1/install.sh | sh
```

Windows users can pass a built package to `python scripts/install.py --package FILE.zip`, or use `install.ps1` with a local checkout and `--binary FILE.exe`.

For fixtures and isolated installs, pass both a separate root and prefix. `--no-activate` prevents native Codex settings from changing:

```sh
python3 scripts/install.py --source . --binary PATH_TO_CTRLISSUES \
  --codex-root /tmp/ctrlissues-codex --prefix /tmp/ctrlissues-data \
  --no-activate --bin-dir /tmp/ctrlissues-bin
```

`--package ZIP` replaces `--source DIR`; the package already contains its binary. `--binary FILE` is required only with source input. A custom `--prefix` requires `--no-activate`; active hook definitions use Codex's standard `${PLUGIN_DATA}` location. The installer places an owned launcher in `~/.local/bin` on Unix (or `~/bin` on Windows) and at the native hook data path. The stable data launcher selects `current`, an atomic pointer to a retained versioned payload. `--rollback` selects the recorded previous payload and retains the shared task database and reports. `--uninstall` unregisters CtrlIssues but retains private task data. `--migrate-graphfather` copies the old GraphFather SQLite database with SQLite backup only when the new `state.sqlite` does not exist.

## Optional browser runtime

The browser feature needs `uv`, Python 3.12, and a named Browser Harness daemon
already using the local Brave endpoint. Install the locked optional runtime
outside ENVSTACK:

```sh
CTRLISSUES_DATA="${CODEX_HOME:-$HOME/.codex}/plugins/data/ctrlissues-ctrlissues"
CTRLISSUES_PAYLOAD=$(cat "$CTRLISSUES_DATA/current")
CTRLISSUES_PLUGIN_ROOT="$CTRLISSUES_DATA/$CTRLISSUES_PAYLOAD/plugins/ctrlissues"
python3 "$CTRLISSUES_PLUGIN_ROOT/hands/bootstrap.py" \
  --data-dir "$CTRLISSUES_DATA" \
  --plugin-root "$CTRLISSUES_PLUGIN_ROOT"
```

Start or verify the existing `ctrlissues` daemon separately in a clean
environment with `BU_NAME=ctrlissues` and
`BU_CDP_URL=http://127.0.0.1:9222`. Do not start it inside a provider run. The
Rust `ctrlissues browser` route invokes `envstack run --` in the foreground and
does not load dotenv files. A compatible `envstack` command must be on `PATH`
for that route, including on Windows; the native package matrix does not by
itself prove the browser or provider route on any platform. See the
[browser reference](../plugins/ctrlissues/skills/ctrlissues/references/hands.md)
for mission limits and unsupported page features.

Package construction makes no network, provider, or credential calls. Direct source/package installation uses the supplied local files; the release bootstrap makes two HTTPS downloads (package and checksum). Native Codex registration requires the installed Codex CLI.
