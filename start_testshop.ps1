<#
    Starts the pretend shop used to test the alerts, and opens its admin page.

        .\start_testshop.ps1

    Leave the window open while testing; close it (or press Ctrl+C) to stop.
    The shop only runs on this computer: http://localhost:8765
#>

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$candidates = @(
    "$env:USERPROFILE\.venvs\cardcopy\Scripts\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
)
$python = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $python) {
    Write-Error "Could not find Python on this machine."
    exit 3
}

# Open the admin page a moment after the server starts.
Start-Job -ScriptBlock { Start-Sleep -Seconds 2; Start-Process "http://localhost:8765/admin" } | Out-Null

& $python (Join-Path $here 'testshop\server.py')
