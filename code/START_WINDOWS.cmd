@echo off
setlocal
set "APP=%~dp0dist\GLD80 MCU Bridge\GLD80 MCU Bridge.exe"
if exist "%APP%" (
    start "" "%APP%"
    exit /b 0
)
echo The Windows application has not been built yet.
echo INSTALL_WINDOWS.cmd will now be started.
call "%~dp0INSTALL_WINDOWS.cmd"
