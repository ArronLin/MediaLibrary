@echo off
setlocal EnableExtensions
REM ============================================================
REM  MediaLibrary Installer (green package)
REM  - Copies the app to %LOCALAPPDATA%\MediaLibrary
REM  - Never touches your existing "data" folder on upgrade
REM  - Creates a desktop shortcut "MediaLibrary" with app icon
REM  Run this script from the extracted package folder.
REM ============================================================

set "SRC=%~dp0"
set "TARGET=%LOCALAPPDATA%\MediaLibrary"

echo.
echo  ==============================================
echo   MediaLibrary Installer
echo   Source : %SRC%
echo   Target : %TARGET%
echo  ==============================================
echo.

if not exist "%SRC%app" (
    echo  [ERROR] "app" folder not found next to this script.
    echo          Please run install.bat from the extracted package folder.
    pause
    exit /b 1
)
if not exist "%SRC%runtime\python.exe" (
    echo  [ERROR] "runtime\python.exe" not found.
    echo          The package should ship with an embedded Python runtime.
    pause
    exit /b 1
)

echo  [1/3] Copying application files ...
robocopy "%SRC%app"     "%TARGET%\app"     /E /NFL /NDL /NJH /NJS >nul
robocopy "%SRC%web"     "%TARGET%\web"     /E /NFL /NDL /NJH /NJS >nul
robocopy "%SRC%runtime" "%TARGET%\runtime" /E /NFL /NDL /NJH /NJS >nul
copy /Y "%SRC%run.bat"   "%TARGET%\run.bat"   >nul
copy /Y "%SRC%app.ico"   "%TARGET%\app.ico"   >nul
copy /Y "%SRC%README.md" "%TARGET%\README.md" >nul
if not exist "%TARGET%\config.json" copy /Y "%SRC%config.json" "%TARGET%\config.json" >nul

echo  [2/3] Creating desktop shortcut ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "$desk = [Environment]::GetFolderPath('Desktop');" ^
  "$lnk = $ws.CreateShortcut((Join-Path $desk 'MediaLibrary.lnk'));" ^
  "$lnk.TargetPath = '%TARGET%\run.bat';" ^
  "$lnk.WorkingDirectory = '%TARGET%';" ^
  "$lnk.IconLocation = '%TARGET%\app.ico,0';" ^
  "$lnk.Description = 'MediaLibrary - local movie poster wall';" ^
  "$lnk.Save()"

echo  [3/3] Done.
echo.
echo  Installed to : %TARGET%
echo  Shortcut     : Desktop\MediaLibrary.lnk  (double-click to launch)
echo  Stop server  : close the black console window that opens on launch
echo.

choice /C YN /M "Launch MediaLibrary now"
if errorlevel 2 goto :end
start "" "%TARGET%\run.bat"

:end
endlocal
