@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 虚拟环境刻意放在技能目录之外（见 scripts/install.py 的说明）。
rem 换过位置的话，设一下 TOKEN_SAVER_HOME 就行。
if not defined TOKEN_SAVER_HOME set "TOKEN_SAVER_HOME=%USERPROFILE%\.token-saver"
set "PY=%TOKEN_SAVER_HOME%\venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [x] 找不到虚拟环境：%PY%
    echo     请先在技能目录里运行:  python scripts\install.py
    pause
    exit /b 1
)

rem `-B` = 不写 .pyc：这个目录是要打包分发的，缓存属于运行期产物
rem （和 data/、venv/ 一样都该落在代码目录之外）。代价只是每次启动多编译一遍，
rem 对常驻进程可以忽略。用命令行开关而不是 PYTHONDONTWRITEBYTECODE 环境变量，
rem 是因为开关的行为一眼可见、也更好验证。
echo ============================================
echo   Token Saver - 简单任务外包给网页端
echo ============================================
echo.
echo   控制台地址: http://127.0.0.1:8787
echo   停止服务:   在这个窗口按 Ctrl+C
echo   想叫停正在跑的批量任务: 控制台右上角「叫停全部」
echo.

"%PY%" -B "%~dp0server.py"

endlocal
