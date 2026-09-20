# Bootstrap for the Telegram Agent Bot on Windows.
#
# Checks for Python and the Claude Code CLI, installs whichever is missing,
# makes sure you're logged into Claude, then hands off to setup.py for the
# interactive part (connecting your Telegram bot). Safe to re-run any time.

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Banner($text) {
    Write-Host ""
    Write-Host ("=" * 64)
    Write-Host "  $text"
    Write-Host ("=" * 64)
}

function Write-Ok($text) {
    Write-Host "   ok  $text" -ForegroundColor Green
}

function Write-Info($text) {
    Write-Host "   $text"
}

function Refresh-Path {
    # A native installer (winget, claude's own installer) updates the
    # persistent PATH in the registry, but this already-running PowerShell
    # process doesn't see that change until we re-read it ourselves.
    $machine = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Test-Command($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

Write-Banner "Telegram Agent Bot -- Setup"
Write-Host "This will check for Python and Claude Code, install whichever is"
Write-Host "missing, then walk you through connecting your Telegram bot."
Write-Host "You can stop at any time with Ctrl+C -- nothing is changed until"
Write-Host "the very last step."

# --- Step 1: Python -----------------------------------------------------
Write-Host "`n[1/3] Checking for Python..."

$pythonCmd = $null
foreach ($candidate in @("py", "python")) {
    if (Test-Command $candidate) {
        $pythonCmd = $candidate
        break
    }
}

if (-not $pythonCmd) {
    Write-Info "Python not found -- installing via winget..."
    if (Test-Command "winget") {
        winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
        Refresh-Path
        foreach ($candidate in @("py", "python")) {
            if (Test-Command $candidate) { $pythonCmd = $candidate; break }
        }
    }
    if (-not $pythonCmd) {
        Write-Host ""
        Write-Host "   Could not install Python automatically. Please install it yourself:"
        Write-Host "     https://python.org/downloads (check 'Add python.exe to PATH')"
        Write-Host "   Then close this window, re-open a terminal, and run setup.bat again."
        Read-Host "Press Enter to exit"
        exit 1
    }
}
Write-Ok "Python found ($pythonCmd)"

# --- Step 2: Claude Code CLI ---------------------------------------------
Write-Host "`n[2/3] Checking for Claude Code..."

if (-not (Test-Command "claude")) {
    Write-Info "Claude Code not found -- installing (this is Anthropic's own"
    Write-Info "installer, run directly from claude.ai)..."
    Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression
    Refresh-Path
    if (-not (Test-Command "claude")) {
        Write-Host ""
        Write-Host "   Claude Code was installed, but this window can't see it yet."
        Write-Host "   Close this window, open a new terminal, and run setup.bat again."
        Read-Host "Press Enter to exit"
        exit 1
    }
}
Write-Ok "Claude Code found"

# --- Step 3: hand off to the interactive Python wizard -------------------
Write-Host "`n[3/3] Continuing setup..."
$ErrorActionPreference = "Continue"
if ($pythonCmd -eq "py") {
    & py -3 "$ScriptDir\setup.py"
} else {
    & python "$ScriptDir\setup.py"
}
