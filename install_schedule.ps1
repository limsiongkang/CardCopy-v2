<#
    Sets up the daily 7:00 am run in Windows Task Scheduler.

    Run this ONCE, after you have filled in .env and tested by
    double-clicking CompetitorMonitor.exe and choosing 2 (Test run).

    Usage:
        .\install_schedule.ps1                  run at 7:00 am while you are logged in
        .\install_schedule.ps1 -Time "06:30"    a different time
        .\install_schedule.ps1 -RunWhenLoggedOff
                                                also run when you are logged out
                                                (the PC still has to be switched on)
        .\install_schedule.ps1 -Remove          delete the scheduled task
#>

[CmdletBinding()]
param(
    [string]$Time = "07:00",
    [switch]$RunWhenLoggedOff,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

$taskName = "Competitor Monitor - Daily"
$here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$script   = Join-Path $here 'monitor.py'

# ---------------------------------------------------------------- remove
if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "Removed the scheduled task '$taskName'." -ForegroundColor Green
    } else {
        Write-Host "There was no scheduled task called '$taskName'." -ForegroundColor Yellow
    }
    exit 0
}

# ---------------------------------------------------------------- checks
if (-not (Test-Path $script)) {
    Write-Error "monitor.py was not found next to this script. Keep all the files in one folder."
    exit 3
}

if (-not (Test-Path (Join-Path $here '.env'))) {
    Write-Warning "There is no .env file yet. The 7am run will stop with an error until you create one."
    Write-Warning "See README.md, or copy .env.example to .env and fill it in."
}

$exe = Join-Path $here 'CompetitorMonitor.exe'
if (-not (Test-Path $exe)) {
    Write-Error "CompetitorMonitor.exe was not found. Double-click build_exe.bat to create it, then run this again."
    exit 3
}

try {
    $parsed = [datetime]::ParseExact($Time, 'HH:mm', $null)
} catch {
    Write-Error "-Time must look like 07:00 (24-hour clock). You gave '$Time'."
    exit 3
}

# ---------------------------------------------------------------- build
# Run the exe directly - no PowerShell in the chain, so PowerShell's execution
# policy can never block the daily run. --scheduled matters: it skips the menu
# and the "press Enter to close" prompt, which would otherwise leave the 7am
# run waiting for a keypress forever.
$action = New-ScheduledTaskAction `
    -Execute $exe `
    -Argument "--scheduled" `
    -WorkingDirectory $here

$trigger = New-ScheduledTaskTrigger -Daily -At $parsed

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -MultipleInstances IgnoreNew

if ($RunWhenLoggedOff) {
    # S4U runs without storing your password, logged in or not.
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType S4U `
        -RunLevel Limited
} else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Limited
}

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Checks competitor prices and stock, writes them to Google Sheets and emails a summary." `
    -Force | Out-Null

Write-Host ""
Write-Host "Scheduled." -ForegroundColor Green
Write-Host "  Task name : $taskName"
Write-Host "  Runs      : every day at $Time"
Write-Host "  Command   : $exe --scheduled"
Write-Host "  Folder    : $here"
if ($RunWhenLoggedOff) {
    Write-Host "  Mode      : runs whether or not you are logged in (PC must be switched on)"
} else {
    Write-Host "  Mode      : runs while you are logged in"
}
Write-Host ""
Write-Host "If the PC is off at $Time, the run happens the next time it is switched on."
Write-Host ""
Write-Host "To test it right now without waiting:"
Write-Host "    Start-ScheduledTask -TaskName '$taskName'"
Write-Host ""
Write-Host "To see it in the Windows interface: press Start, type 'Task Scheduler'."
Write-Host "To remove it:  .\install_schedule.ps1 -Remove"
Write-Host ""
