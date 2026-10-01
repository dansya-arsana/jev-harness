# Jev harness installer wrapper: finds Python 3.9+ and runs scripts/onboard.py.
# -Legacy / --legacy runs the old installer (scripts\legacy\install.ps1).
# JEV_ONBOARD_PY / JEV_LEGACY_SCRIPT override the targets (tests only).
$ErrorActionPreference = 'Stop'

$fwd = @($args)
if ($fwd | Where-Object { $_ -ieq '-Legacy' -or $_ -ieq '--legacy' }) {
  $rest = @($fwd | Where-Object { $_ -ine '-Legacy' -and $_ -ine '--legacy' })
  $leg = if ($env:JEV_LEGACY_SCRIPT) { $env:JEV_LEGACY_SCRIPT } else { Join-Path $PSScriptRoot 'scripts\legacy\install.ps1' }
  & $leg @rest
  exit $LASTEXITCODE
}

$candidates = @(
  @{Cmd='py';      Args=@('-3')},
  @{Cmd='python';  Args=@()},
  @{Cmd='python3'; Args=@()}
)
$check = 'import sys; print(sys.executable) if sys.version_info >= (3, 9) else sys.exit(1)'
$exe = $null; $pre = @()
foreach ($c in $candidates) {
  if (-not (Get-Command $c.Cmd -ErrorAction SilentlyContinue)) { continue }
  $out = $null
  try { $out = & $c.Cmd @($c.Args) -c $check 2>$null } catch { continue }
  if ($LASTEXITCODE -eq 0 -and $out) {
    $p = ("$out").Trim()
    if (Test-Path -LiteralPath $p) { $exe = $p; break }
  }
}
if (-not $exe) {
  Write-Error "jev-harness needs Python 3.9+. Install it from python.org (the Microsoft Store 'python' stub does not work), then re-run."
  exit 1
}
$target = if ($env:JEV_ONBOARD_PY) { $env:JEV_ONBOARD_PY } else { Join-Path $PSScriptRoot 'scripts\onboard.py' }
& $exe $target @fwd
exit $LASTEXITCODE
