param(
    [Parameter(Mandatory = $true)]
    [string]$WorktreePath,
    [string]$TaskName = "EffectBrowserAutobuild",
    [int]$IntervalMinutes = 30
)

$ErrorActionPreference = "Stop"
if ($IntervalMinutes -lt 15) {
    throw "IntervalMinutes must be at least 15"
}

$resolvedWorktree = (Resolve-Path -LiteralPath $WorktreePath).Path
$repository = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $repository "automation\run-autobuild.ps1"
$powerShell = (Get-Command powershell.exe).Source
$escapedRunner = '"' + $runner + '"'
$escapedWorktree = '"' + $resolvedWorktree + '"'
$arguments = (
    "-NoProfile -NonInteractive -ExecutionPolicy Bypass " +
    "-File $escapedRunner -WorktreePath $escapedWorktree"
)

$action = New-ScheduledTaskAction -Execute $powerShell -Argument $arguments
$start = (Get-Date).AddMinutes(10)
$trigger = New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Continue one guarded Effect Browser roadmap slice per run." `
    -Force | Out-Null

Get-ScheduledTask -TaskName $TaskName |
    Select-Object TaskName, State, TaskPath
