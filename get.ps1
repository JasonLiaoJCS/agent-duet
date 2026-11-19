# Agent Duet: download (or update) and install in one step.
#   irm https://raw.githubusercontent.com/JasonLiaoJCS/agent-duet/main/get.ps1 | iex
$ErrorActionPreference = "Stop"
$dir = if ($env:AGENT_DUET_DIR) { $env:AGENT_DUET_DIR } else { Join-Path $HOME "agent-duet" }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "git is needed. Install Git for Windows, then run this again." }
$python = $null
foreach ($candidate in @(@("py", "-3"), @("python"), @("python3"))) {
    if (Get-Command $candidate[0] -ErrorAction SilentlyContinue) { $python = $candidate; break }
}
if (-not $python) { throw "Python 3.10+ is needed. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again." }
if (Test-Path (Join-Path $dir ".git")) {
    Write-Host "Updating $dir ..."
    git -C $dir pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "Could not update $dir (git said so above). Nothing was installed." }
} else {
    Write-Host "Downloading Agent Duet to $dir ..."
    git clone --depth 1 https://github.com/JasonLiaoJCS/agent-duet.git $dir
    if ($LASTEXITCODE -ne 0) { throw "Could not download Agent Duet (git said so above). Nothing was installed." }
}
$installer = Join-Path $dir "agent-duet\scripts\install.py"
$rest = @()
if ($python.Count -gt 1) { $rest = $python[1..($python.Count - 1)] }
& $python[0] @rest -X utf8 $installer --auto-update on
if ($LASTEXITCODE -ne 0) { throw "The installer reported an error; read the message above." }
