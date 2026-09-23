#!/bin/sh
# Joined proof entrypoint. Do not run during construction.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
cargo test --manifest-path plugins/ctrlissues/runtime/Cargo.toml
python3 -m unittest discover -s tests -p 'test_install.py' -v
python3 -m unittest discover -s tests/backend -v
python3 -m pytest tests/hands -v
python3 scripts/validate-package.py --source .
