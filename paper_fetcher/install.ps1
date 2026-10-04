# Sets up the Research Paper Fetcher for the current Windows user:
# finds Python, creates the "Paper Search Settings" shortcut, schedules a daily run, and opens settings.
param([string]$Time = "9:00AM")
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$root = Split-Path $here
$taskName = "Research Paper Fetcher"

function Find-Python {
    foreach ($cmd in @("py", "python")) {
        $c = Get-Command $cmd -ErrorAction SilentlyContinue
        if (-not $c -or $c.Source -like "*WindowsApps*") { continue }  # skip the Microsoft Store stub
        try {
            $exe = & $c.Source -c "import sys, tkinter; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
        } catch {}
    }
    return $null
}

Write-Host "Research Paper Fetcher setup" -ForegroundColor Cyan
$python = Find-Python
if (-not $python) {
    Write-Host "Python 3 (with Tkinter) was not found."
    $ans = Read-Host "Install Python 3.13 for your user account now with winget? (Y/N)"
    if ($ans -match '^[Yy]') {
        winget install --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
        $python = Find-Python
    }
    if (-not $python) {
        Write-Host "Please install Python 3 from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run Install.bat again." -ForegroundColor Yellow
        exit 1
    }
}
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }
Write-Host "Using Python: $python"

# Shortcut to the settings window, next to Install.bat
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut((Join-Path $root "Paper Search Settings.lnk"))
$lnk.TargetPath = $pythonw
$lnk.Arguments = "`"$here\settings.pyw`""
$lnk.WorkingDirectory = $here
$lnk.Description = "Choose what the daily paper search looks for and how many papers per day"
$lnk.Save()

# Daily scheduled task (current user, no admin needed)
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$here\fetch_papers.py`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Downloads new open-access research papers daily into $root" -Force | Out-Null
$next = (Get-ScheduledTask $taskName | Get-ScheduledTaskInfo).NextRunTime
Write-Host "Scheduled daily run '$taskName' (next: $next)."
Write-Host "Papers will be saved in topic folders inside: $root"
Write-Host ""
Write-Host "Opening Paper Search Settings: add your own search (or tick a preset), set papers per day, then Save." -ForegroundColor Green
Start-Process $pythonw -ArgumentList "`"$here\settings.pyw`"" -WorkingDirectory $here
