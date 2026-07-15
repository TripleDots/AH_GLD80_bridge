@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title GLD80 MCU Bridge - Windows installation

echo.
echo ============================================================
echo   GLD80 MCU Bridge - automatic Windows installation
echo ============================================================
echo.
echo This builds a portable Windows application with an .exe.
echo On the first run, Python and the required packages are downloaded.
echo.

call :find_python
if not defined PYEXE (
    echo Python 3.10-3.12 was not found.
    where winget.exe >nul 2>&1
    if errorlevel 1 (
        echo.
        echo Windows Package Manager ^(winget^) is not available.
        echo Install Python 3.12 from python.org, then run this file again.
        echo.
        pause
        exit /b 1
    )
    echo Python 3.12 will now be installed automatically...
    winget install --exact --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo.
        echo The automatic Python installation failed.
        echo Install Python 3.12 manually and try again.
        pause
        exit /b 1
    )
    call :find_python
)

if not defined PYEXE (
    echo Python was installed but could not yet be found.
    echo Sign out and back in, then try again.
    pause
    exit /b 1
)

echo Python: "%PYEXE%"
"%PYEXE%" -c "import sys; assert (3,10) <= sys.version_info[:2] < (3,13), 'Use Python 3.10, 3.11 or 3.12'"
if errorlevel 1 (
    echo.
    echo The detected Python version is not supported.
    echo Install Python 3.12 and try again.
    pause
    exit /b 1
)

set "VENV=%~dp0.build-venv"
if not exist "%VENV%\Scripts\python.exe" (
    echo.
    echo Creating a private build environment...
    "%PYEXE%" -m venv "%VENV%"
    if errorlevel 1 goto :failed
)

set "VPY=%VENV%\Scripts\python.exe"
echo.
echo Installing or updating required packages...
"%VPY%" -m pip install --disable-pip-version-check --upgrade pip wheel
if errorlevel 1 goto :failed
"%VPY%" -m pip install --disable-pip-version-check -r requirements.txt "pyinstaller>=6.5,<7"
if errorlevel 1 goto :failed

echo.
echo Building the Windows application. This can take several minutes...
if exist build rmdir /s /q build
if exist "dist\GLD80 MCU Bridge" rmdir /s /q "dist\GLD80 MCU Bridge"
"%VENV%\Scripts\pyinstaller.exe" --noconfirm --clean GLD80-MCU-Bridge.spec
if errorlevel 1 goto :failed

set "APP=%~dp0dist\GLD80 MCU Bridge\GLD80 MCU Bridge.exe"
if not exist "%APP%" goto :failed

copy /y README.md "dist\GLD80 MCU Bridge\README.md" >nul
copy /y CHANGELOG.md "dist\GLD80 MCU Bridge\CHANGELOG.md" >nul
if exist "dist\GLD80 MCU Bridge\integrations" rmdir /s /q "dist\GLD80 MCU Bridge\integrations"
xcopy /e /i /y integrations "dist\GLD80 MCU Bridge\integrations" >nul

set "PS_SCRIPT=%TEMP%\gld80_shortcut_%RANDOM%.ps1"
>"%PS_SCRIPT%" echo $desktop = [Environment]::GetFolderPath('Desktop')
>>"%PS_SCRIPT%" echo $shell = New-Object -ComObject WScript.Shell
>>"%PS_SCRIPT%" echo $shortcut = $shell.CreateShortcut((Join-Path $desktop 'GLD80 MCU Bridge.lnk'))
>>"%PS_SCRIPT%" echo $shortcut.TargetPath = '%APP:'=''%'
>>"%PS_SCRIPT%" echo $shortcut.WorkingDirectory = '%~dp0dist\GLD80 MCU Bridge'
>>"%PS_SCRIPT%" echo $shortcut.IconLocation = '%APP:'=''%'
>>"%PS_SCRIPT%" echo $shortcut.Description = 'GLD80 MCU, HUI and Raw MIDI DAW bridge'
>>"%PS_SCRIPT%" echo $shortcut.Save()
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" >nul 2>&1
del /q "%PS_SCRIPT%" >nul 2>&1

echo.
echo ============================================================
echo Installation complete.
echo.
echo Application folder:
echo   %~dp0dist\GLD80 MCU Bridge
echo.
echo A desktop shortcut has also been created.
echo ============================================================
echo.
start "" "%APP%"
pause
exit /b 0

:find_python
set "PYEXE="
for %%V in (3.12 3.11 3.10) do (
    if not defined PYEXE (
        for /f "usebackq delims=" %%P in (`py -%%V -c "import sys; print(sys.executable)" 2^>nul`) do set "PYEXE=%%P"
    )
)
if not defined PYEXE if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PYEXE if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\Python311\python.exe"
if not defined PYEXE if exist "%LocalAppData%\Programs\Python\Python310\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\Python310\python.exe"
if not defined PYEXE (
    for /f "delims=" %%P in ('where python.exe 2^>nul') do if not defined PYEXE set "PYEXE=%%P"
)
exit /b 0

:failed
echo.
echo ============================================================
echo The build or installation failed.
echo Take a screenshot of the final error lines for troubleshooting.
echo ============================================================
echo.
pause
exit /b 1
