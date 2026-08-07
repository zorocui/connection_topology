@echo off
setlocal

set "POWERSHELL_SCRIPT=%~dp0package-linux.ps1"

if not exist "%POWERSHELL_SCRIPT%" (
    echo ERROR: package-linux.ps1 was not found beside this launcher.
    set "EXIT_CODE=1"
    goto :finish
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%POWERSHELL_SCRIPT%" %*
set "EXIT_CODE=%ERRORLEVEL%"

if "%EXIT_CODE%"=="0" (
    echo.
    echo Linux deployment package created successfully.
) else (
    echo.
    echo ERROR: Packaging failed with exit code %EXIT_CODE%.
)

:finish
echo.
pause
exit /b %EXIT_CODE%
