@echo off
setlocal EnableDelayedExpansion
title CEC4HTPC Installer

echo ============================================================
echo  CEC4HTPC ^| Windows Startup Installer
echo ============================================================
echo.

REM ── locate pythonw.exe (no console window) ─────────────────────────────────
set "PYTHONW="

REM Check common install locations first
for %%P in (
    "%LocalAppData%\Programs\Python\Python313\pythonw.exe"
    "%LocalAppData%\Programs\Python\Python312\pythonw.exe"
    "%LocalAppData%\Programs\Python\Python311\pythonw.exe"
    "%LocalAppData%\Programs\Python\Python310\pythonw.exe"
    "C:\Python313\pythonw.exe"
    "C:\Python312\pythonw.exe"
    "C:\Python311\pythonw.exe"
) do (
    if exist %%P (
        set "PYTHONW=%%~P"
        goto :found_python
    )
)

REM Fall back to PATH lookup
for /f "delims=" %%i in ('where pythonw 2^>nul') do (
    set "PYTHONW=%%i"
    goto :found_python
)

echo ERROR: pythonw.exe not found. Install Python 3.10+ and try again.
pause & exit /b 1

:found_python
echo Python:  %PYTHONW%

set "SCRIPT=%~dp0cec4htpc.py"
echo Script:  %SCRIPT%
echo.

REM ── install pip dependencies ───────────────────────────────────────────────
echo Installing Python dependencies...
"%PYTHONW%" -m pip install -r "%~dp0requirements.txt" --quiet
if errorlevel 1 (
    echo WARNING: pip install had errors. Some features may not work.
)
echo.

REM ── register Task Scheduler task via PowerShell ───────────────────────────
REM  schtasks /tr can't handle paths with spaces+parentheses via cmd quoting.
REM  PowerShell reads the paths from env vars, which sidesteps the issue.
echo Registering startup task (requires admin for RunLevel Highest)...

set "CEC_PY=!PYTHONW!"
set "CEC_SC=!SCRIPT!"

powershell -NoProfile -ExecutionPolicy Bypass -Command "& { $exe = $env:CEC_PY; $arg = '\"' + $env:CEC_SC + '\"'; $action = New-ScheduledTaskAction -Execute $exe -Argument $arg; $trigger = New-ScheduledTaskTrigger -AtLogOn; $trigger.Delay = New-TimeSpan -Seconds 30; $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest -LogonType Interactive; $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable; Register-ScheduledTask -TaskName 'CEC4HTPC' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null }"

if errorlevel 1 (
    echo.
    echo FAILED. Run this batch file as Administrator and try again.
) else (
    echo.
    echo SUCCESS!
    echo CEC4HTPC will launch automatically 30 seconds after login.
    echo.
    echo To start it immediately, run:
    echo   "!PYTHONW!" "!SCRIPT!"
    echo.
    echo To change settings, edit:
    echo   "%~dp0config.json"
)

echo.
pause
