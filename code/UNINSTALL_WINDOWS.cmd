@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo Uninstalling GLD80 MCU Bridge...
taskkill /IM "GLD80 MCU Bridge.exe" /F >nul 2>&1
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "GLD80 MCU Bridge" /f >nul 2>&1
if exist "dist\GLD80 MCU Bridge" rmdir /s /q "dist\GLD80 MCU Bridge"
if exist build rmdir /s /q build
if exist .build-venv rmdir /s /q .build-venv
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p=Join-Path ([Environment]::GetFolderPath('Desktop')) 'GLD80 MCU Bridge.lnk'; if(Test-Path $p){Remove-Item $p -Force}" >nul 2>&1

echo.
choice /C YN /N /M "Also delete saved settings in %%USERPROFILE%%\.gld80_mcu_bridge? [Y/N]: "
if errorlevel 2 goto KEEP_SETTINGS
if exist "%USERPROFILE%\.gld80_mcu_bridge" rmdir /s /q "%USERPROFILE%\.gld80_mcu_bridge"
echo Saved settings were removed.
goto DONE

:KEEP_SETTINGS
echo Saved settings were kept in %%USERPROFILE%%\.gld80_mcu_bridge.

:DONE
echo Windows startup and the desktop shortcut were removed.
pause
