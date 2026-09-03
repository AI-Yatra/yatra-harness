# Install `ay` and `harness` on Windows.
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/AI-Yatra/yatra-harness/main/install.ps1 | iex"
#
# The same shape as install.sh: find a downloader, install uv if the machine
# has none, install this package as a uv tool, then run `ay` to prove the
# result works before saying it does.
#
# uv ships its own CPython and downloads one automatically, so nothing here
# needs Python to already be present.
#
# Settings come from environment variables rather than parameters, because a
# script piped into `iex` cannot be given arguments.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Repo = 'AI-Yatra/yatra-harness'
$Package = 'yatra-harness'
# Not optional. The REPL spawns the harness with its own interpreter, and the
# workshop's spreadsheet task needs openpyxl available there.
$Extra = 'openpyxl'

$Ref = if ($env:AY_REF) { $env:AY_REF } else { 'main' }
$PythonVersion = if ($env:AY_PYTHON) { $env:AY_PYTHON } else { '3.12' }
$Source = if ($env:AY_SOURCE) { $env:AY_SOURCE } else { '' }
$DryRun = $env:AY_DRY_RUN -eq '1'

function Write-Step { param([string]$Text) Write-Host "==> " -ForegroundColor Cyan -NoNewline; Write-Host $Text }
function Write-Detail { param([string]$Text) Write-Host "  $Text" }
function Write-Muted { param([string]$Text) Write-Host "  $Text" -ForegroundColor DarkGray }
function Stop-Install {
    param([string]$Text)
    Write-Host ""
    Write-Host "error: $Text" -ForegroundColor Red
    exit 1
}

# Returns true when the URL answers, used to tell a published package from one
# that is not on PyPI yet so the same script works before and after publishing.
function Test-Url {
    param([string]$Url)
    try {
        Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 20 -Method Head | Out-Null
        return $true
    } catch {
        return $false
    }
}

# ------------------------------------------------------------------------ uv

Write-Host ""
Write-Step "Looking for uv"
$Uv = $null
$found = Get-Command uv -ErrorAction SilentlyContinue
if ($found) {
    $Uv = $found.Source
    Write-Detail "found $(& $Uv --version)"
} elseif (Test-Path "$env:USERPROFILE\.local\bin\uv.exe") {
    $Uv = "$env:USERPROFILE\.local\bin\uv.exe"
    Write-Detail "found $(& $Uv --version) at $Uv"
} else {
    Write-Detail "not installed, fetching it from astral.sh"
    if ($DryRun) {
        Write-Muted "(dry run: skipping)"
        $Uv = 'uv'
    } else {
        try {
            Invoke-RestMethod -Uri 'https://astral.sh/uv/install.ps1' -UseBasicParsing | Invoke-Expression
        } catch {
            Stop-Install "could not install uv. Install it yourself from https://docs.astral.sh/uv/ and run this again."
        }
        if (Test-Path "$env:USERPROFILE\.local\bin\uv.exe") {
            $Uv = "$env:USERPROFILE\.local\bin\uv.exe"
        } else {
            $found = Get-Command uv -ErrorAction SilentlyContinue
            if (-not $found) { Stop-Install "uv installed but could not be found afterwards." }
            $Uv = $found.Source
        }
        Write-Detail "installed $(& $Uv --version)"
    }
}

# --------------------------------------------------------------------- source

Write-Step "Choosing a source"
if ($Source) {
    Write-Detail "$Source (from AY_SOURCE)"
} elseif (Test-Url "https://pypi.org/simple/$Package/") {
    $Source = $Package
    Write-Detail "PyPI: $Package"
} else {
    # Not on PyPI yet. A source tarball needs no git binary, and one more
    # prerequisite is one more thing to go wrong.
    $Source = "https://github.com/$Repo/archive/refs/heads/$Ref.tar.gz"
    Write-Detail "GitHub: $Repo@$Ref (not on PyPI yet)"
}

# -------------------------------------------------------------------- install

Write-Step "Installing"
Write-Detail "python $PythonVersion, with $Extra"
if ($DryRun) {
    Write-Host ""
    Write-Muted "dry run, nothing was changed. Would have run:"
    Write-Detail "$Uv tool install --force --python $PythonVersion --with $Extra $Source"
    Write-Host ""
    exit 0
}

& $Uv tool install --force --python $PythonVersion --with $Extra $Source 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Stop-Install "the install failed. To see why, run it directly:`n       $Uv tool install --force --python $PythonVersion --with $Extra $Source"
}

$Bin = (& $Uv tool dir --bin).Trim()
if (-not $Bin) { $Bin = "$env:USERPROFILE\.local\bin" }
$Ay = Join-Path $Bin 'ay.exe'
if (-not (Test-Path $Ay)) {
    Stop-Install "installed, but $Ay is missing. This is a packaging fault, not your machine."
}

# ---------------------------------------------------------------- verification

# The step that matters. An earlier version of this package installed cleanly
# and then failed on first run, because its default config was not inside the
# wheel. Anything that only checks "did the install command succeed" would have
# reported success. Starting the REPL and exiting loads the config, resolves a
# route and builds the prompt.
Write-Step "Checking it runs"
$output = '/exit' | & $Ay 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Detail "ay starts and loads its config"
} else {
    Write-Host ""
    Write-Host "  ay was installed but does not start. Output:" -ForegroundColor Red
    $output | ForEach-Object { Write-Host "  $_" }
    Stop-Install "install incomplete."
}

# ---------------------------------------------------------------------- finish

Write-Host ""
Write-Step "Done"
Write-Detail "ay        $Ay"
Write-Detail "harness   $(Join-Path $Bin 'harness.exe')"

$onPath = ($env:PATH -split ';') -contains $Bin
Write-Host ""
if ($onPath) {
    Write-Detail "Run 'ay' in any directory to start."
} else {
    Write-Host "  $Bin is not on your PATH yet." -ForegroundColor Yellow
    Write-Detail "Add it for this session:"
    Write-Muted "    `$env:PATH = `"$Bin;`$env:PATH`""
    Write-Detail "Or permanently, for every new session:"
    Write-Muted "    $Uv tool update-shell"
}
Write-Host ""
Write-Muted "First run needs no API key: ay --model local, or see the README."
Write-Host ""
