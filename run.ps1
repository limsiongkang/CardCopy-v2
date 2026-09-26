<#
    Runs the competitor monitor.

    Examples:
        .\run.ps1                 normal run: scrape, write to the sheet, email
        .\run.ps1 -DryRun         scrape and show results, write nothing, send nothing
        .\run.ps1 -Limit 3        only check the first 3 URLs
        .\run.ps1 -NoEmail        write to the sheet but skip the email
        .\run.ps1 -AlwaysEmail    email me even when nothing changed
        .\run.ps1 -Url "https://..."   check one page, for testing
#>

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$NoEmail,
    [switch]$AlwaysEmail,
    [int]$Limit,
    [string]$Url,
    [switch]$Test
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# Find a usable Python. The first entry is the one this was set up with.
#
# The virtual environment comes first: it is the only one with Scrapling,
# gspread and the fetcher's browsers actually installed. A bare system
# Python will fail on the first import, so preferring it would only produce
# a confusing crash.
$candidates = @(
    "$env:USERPROFILE\.venvs\cardcopy\Scripts\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
)
$python = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $python) {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $python = $py.Source
        $prefix = @('-3')
    }
}

if (-not $python) {
    Write-Error "Could not find Python on this machine. Install Python 3.10 or newer from python.org, then run setup again."
    exit 3
}

$arguments = @()
if ($prefix) { $arguments += $prefix }
$arguments += (Join-Path $here 'monitor.py')

if ($DryRun)      { $arguments += '--dry-run' }
if ($NoEmail)     { $arguments += '--no-email' }
if ($AlwaysEmail) { $arguments += '--always-email' }
if ($Limit)       { $arguments += @('--limit', $Limit) }
if ($Url)         { $arguments += @('--url', $Url) }
if ($Test)        { $arguments += '--test' }

& $python @arguments
exit $LASTEXITCODE
