@echo off
title CEC4HTPC Uninstaller

echo Removing CEC4HTPC startup task...
schtasks /delete /tn "CEC4HTPC" /f

if errorlevel 1 (
    echo Task not found or could not be removed.
) else (
    echo Done. CEC4HTPC will no longer start automatically.
)

echo.
pause
