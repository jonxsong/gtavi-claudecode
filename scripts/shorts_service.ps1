param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Install', 'Start', 'Stop', 'Status')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repository '.venv\Scripts\python.exe'
$entryPoint = Join-Path $PSScriptRoot 'shorts_engine.py'
$taskName = 'GTA VIBING Shorts Engine'

switch ($Action) {
    'Install' {
        if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
            throw 'Create the repository Python virtual environment first.'
        }
        $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $taskAction = New-ScheduledTaskAction -Execute $python -Argument ('"{0}" run' -f $entryPoint) -WorkingDirectory $repository
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
            -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger `
            -Principal $principal -Settings $settings -Force | Out-Null
        Start-ScheduledTask -TaskName $taskName
    }
    'Start' { Start-ScheduledTask -TaskName $taskName }
    'Stop' { Stop-ScheduledTask -TaskName $taskName }
    'Status' {
        $task = Get-ScheduledTask -TaskName $taskName
        $info = Get-ScheduledTaskInfo -TaskName $taskName
        Write-Output ('{0} is {1}. Last run {2:o}, result {3}.' -f $task.TaskName, $task.State, $info.LastRunTime, $info.LastTaskResult)
        & $python $entryPoint status
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
}
