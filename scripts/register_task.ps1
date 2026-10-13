# 注册「每日早上自动发布」到 Windows 任务计划程序
# 用法（管理员 PowerShell）：
#   .\scripts\register_task.ps1
#   .\scripts\register_task.ps1 -Time 07:30 -Count 3
# 卸载：
#   Unregister-ScheduledTask -TaskName "VideoPub-Daily" -Confirm:$false

param(
    [string]$TaskName = "VideoPub-Daily",
    [string]$Time     = "08:00",
    [int]$Count       = 3,
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Bat  = Join-Path $Root "scripts\run_daily.bat"

if (-not (Test-Path $Bat)) { throw "找不到 $Bat" }
if (-not $PythonExe) {
    $venv = Join-Path $Root ".venv\Scripts\python.exe"
    $PythonExe = if (Test-Path $venv) { $venv } else { "python" }
}

# 把发布条数写进配置（保持单一事实来源：config.json）
$cfgPath = Join-Path $Root "config.json"
$cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
$cfg.schedule.daily_count = $Count
$cfg.schedule.time = $Time
$cfg | ConvertTo-Json -Depth 10 | Set-Content $cfgPath -Encoding UTF8

$action  = New-ScheduledTaskAction -Execute $Bat
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "视频号每日自动发布（$Count 条 / $Time）" -Force | Out-Null

Write-Host "已注册计划任务：$TaskName（每日 $Time 发布 $Count 条）" -ForegroundColor Green
Write-Host "查看：Get-ScheduledTask -TaskName $TaskName"
Write-Host "立即试跑：Start-ScheduledTask -TaskName $TaskName"
