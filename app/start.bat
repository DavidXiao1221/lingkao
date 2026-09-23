@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if exist ".venv\Scripts\python.exe" goto dependencies
set "LINGKAO_PYTHON=python"
python --version >nul 2>&1
if errorlevel 1 (
  set "LINGKAO_PYTHON=py -3"
  py -3 --version >nul 2>&1
  if errorlevel 1 goto missing_python
)
%LINGKAO_PYTHON% -m venv .venv
if errorlevel 1 goto bootstrap_error
:dependencies
.venv\Scripts\python.exe -c "import sympy; assert sympy.__version__ == '1.14.0'" >nul 2>&1
if not errorlevel 1 goto run
echo 首次准备本项目依赖，需要联网；安装成功后可离线运行。
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto bootstrap_error
:run
if "%~1"=="--test" goto tests
echo.
echo 服务启动后，在浏览器打开下方显示的本机地址。
.venv\Scripts\python.exe server.py %*
set "LINGKAO_EXIT=%ERRORLEVEL%"
pause
exit /b %LINGKAO_EXIT%
:tests
.venv\Scripts\python.exe -m unittest discover -s tests -v
if errorlevel 1 exit /b %ERRORLEVEL%
where node >nul 2>&1
if errorlevel 1 (
  echo 未安装 Node.js，已跳过前端草稿回归；运行应用不需要 Node.js。
  exit /b 0
)
node tests\test_drafts.cjs
exit /b %ERRORLEVEL%
:missing_python
echo 请先安装 Python 3.10 或更新版本，并启用添加至 PATH。
pause
exit /b 1
:bootstrap_error
echo 环境准备失败，请检查上面的错误；依赖仅安装在本项目 .venv 中。
pause
exit /b 1
