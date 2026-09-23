$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Mode = 'source'
foreach ($Arg in $args) { if ($Arg -in @('--source', '--package', '--rollback', '--uninstall')) { $Mode = 'passthrough' } }
if ($Mode -eq 'source') { & python "$Root/scripts/install.py" --source "$Root" @args }
else { & python "$Root/scripts/install.py" @args }
exit $LASTEXITCODE
