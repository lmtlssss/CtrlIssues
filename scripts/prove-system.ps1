$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Push-Location $Root
try {
  cargo test --manifest-path plugins/ctrlissues/runtime/Cargo.toml
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  python -m unittest discover -s tests -p test_install.py -v
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  python -m unittest discover -s tests/backend -v
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  python -m pytest tests/hands -v
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  python scripts/validate-package.py --source .
  exit $LASTEXITCODE
} finally { Pop-Location }
