@echo off
setlocal
cd /d "%~dp0"

set "PY="

if exist "runtime\python.exe" set "PY=runtime\python.exe"

if not defined PY (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -c "import sys" >nul 2>nul
        if not errorlevel 1 set "PY=py -3"
    )
)

if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 set "PY=python"
)

if not defined PY goto :nopython

echo.
echo   正在启动影视库海报墙...
echo   真实访问地址见下方日志，浏览器会自动打开
echo   关闭此窗口即停止服务
echo.

set "MEDIA_OPEN=1"
%PY% -u -m app.server
if errorlevel 1 goto :failed
goto :end

:failed
echo.
echo  [错误] 服务异常退出，请把上面的报错信息发出来
echo.
pause
goto :end

:nopython
echo.
echo  [错误] 未找到 Python 3.9 或更高版本
echo.
echo  解决方案二选一：
echo   1. 安装 Python 3.9+ 并勾选 "Add Python to PATH"
echo   2. 下载 Windows embeddable 版 Python，解压到本目录下的 runtime\ 文件夹
echo      使 runtime\python.exe 存在，即可零安装运行
echo.
pause

:end
endlocal
