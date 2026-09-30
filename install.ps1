# Windows installer for jev-harness (port of install.sh --hooks). Also merges config/global-rules.md into ~/.claude/CLAUDE.md.
# Safe to re-run: junction/agent copies are refreshed, settings.json is backed up before any change,
# hook entries are only added once.
$ErrorActionPreference = 'Stop'

$Claude  = Join-Path $HOME '.claude'
$Stamp   = Get-Date -Format 'yyyyMMdd-HHmmss'
$Harness = $PSScriptRoot
$Python  = (Get-Command python -ErrorAction Stop).Source -replace '\\', '/'

# --- jev-harness: skill (junction, follows git pull) + agents (copied) ---
$skillDst = Join-Path $Claude 'skills\jev-orchestrator'
New-Item -ItemType Directory -Force (Split-Path $skillDst) | Out-Null
if (Test-Path $skillDst) {
  $item = Get-Item $skillDst -Force
  if ($item.LinkType) { $item.Delete() } else { Move-Item $skillDst (Join-Path $Claude "backups\jev-$Stamp-skill") }
}
New-Item -ItemType Junction -Path $skillDst -Target (Join-Path $Harness 'skill\jev-orchestrator') | Out-Null
Write-Host "linked    $skillDst"

$agentsDst = Join-Path $Claude 'agents'
New-Item -ItemType Directory -Force $agentsDst | Out-Null
Get-ChildItem (Join-Path $Harness 'agents\jev-*.md') | ForEach-Object {
  Copy-Item $_.FullName (Join-Path $agentsDst $_.Name) -Force
  Write-Host "copied    agents\$($_.Name)"
}

$jevDir = Join-Path $Claude 'jev'
New-Item -ItemType Directory -Force $jevDir | Out-Null
if (-not (Test-Path (Join-Path $jevDir 'conditions.json'))) {
  Copy-Item (Join-Path $Harness 'config\conditions.example.json') (Join-Path $jevDir 'conditions.json')
  Write-Host "created   ~/.claude/jev/conditions.json"
}

# --- hooks in ~/.claude/settings.json ---
$settings = Join-Path $Claude 'settings.json'
if (-not (Test-Path $settings)) { '{}' | Set-Content $settings -Encoding utf8 }
Copy-Item $settings "$settings.bak-jev-$Stamp"
$hookDir = ($skillDst -replace '\\', '/') + '/hooks'
$merge = Join-Path $env:TEMP "jev-merge-$Stamp.py"
@'
import json, sys
path, py, hd = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(path, encoding='utf-8-sig'))
hooks = d.setdefault('hooks', {})
def cmd(name): return '"%s" "%s/%s"' % (py, hd, name)
entries = [
    ('PreToolUse', {'matcher': 'Bash|PowerShell|Write|Edit|MultiEdit|NotebookEdit', 'hooks': [{'type': 'command', 'command': cmd('permission_gate.py'), 'timeout': 10}]}),
    ('PreToolUse', {'matcher': 'Agent|Task', 'hooks': [{'type': 'command', 'command': cmd('dispatch_router.py'), 'timeout': 10}]}),
    ('UserPromptSubmit', {'hooks': [{'type': 'command', 'command': cmd('prompt_router.py'), 'timeout': 15}]}),
]
for event, entry in entries:
    lst = hooks.setdefault(event, [])
    c = entry['hooks'][0]['command']
    if any(h.get('command') == c for g in lst for h in g.get('hooks', [])):
        print('hook      %s already registered' % c.rsplit('/', 1)[-1])
    else:
        lst.append(entry); print('hook      %s registered' % c.rsplit('/', 1)[-1])
open(path, 'w', encoding='utf-8').write(json.dumps(d, indent=2) + '\n')
'@ | Set-Content $merge -Encoding utf8
& $Python $merge $settings $Python $hookDir
Remove-Item $merge

# --- one shared TypeSafe key file (read by jev-harness and jev-ultrafast-bora scripts/jev-run.ps1) ---
$keyDir = Join-Path $HOME '.config\typesafe'
$keyFile = Join-Path $keyDir '.env'
New-Item -ItemType Directory -Force $keyDir | Out-Null
if (-not (Test-Path $keyFile)) {
  @"
# TypeSafe / Jev key - shared by jev-harness hooks and jev-ultrafast-bora. Never commit.
TYPESAFE_API_KEY=
TYPESAFE_DEFAULT_MODEL=jev-latest
TYPESAFE_MODEL=jev-latest
"@ | Set-Content $keyFile -Encoding ascii
  Write-Host "created   $keyFile  <- paste your key here"
}

# --- global rules block in ~/.claude/CLAUDE.md (replaced between markers, rest of the file kept) ---
$claudeMd = Join-Path $Claude 'CLAUDE.md'
$block = (Get-Content (Join-Path $Harness 'config\global-rules.md') -Raw).Trim()
$text = if (Test-Path $claudeMd) { Get-Content $claudeMd -Raw } else { '' }
if ($text) { Copy-Item $claudeMd "$claudeMd.bak-jev-$Stamp" }
$pattern = '(?s)<!-- jev-harness:begin.*?<!-- jev-harness:end -->'
if ($text -match $pattern) {
  $text = [regex]::Replace($text, $pattern, { param($m) $block })
  Write-Host "updated   rules block in $claudeMd"
} else {
  $text = ($text.TrimEnd() + "`r`n`r`n" + $block).TrimStart()
  Write-Host "added     rules block to $claudeMd"
}
[IO.File]::WriteAllText($claudeMd, $text.TrimEnd() + "`r`n", (New-Object Text.UTF8Encoding $false))

Write-Host "`ndone. Start a new Claude Code session so it loads the agents and hooks."
