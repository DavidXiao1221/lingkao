@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
set "LINGKAO_DB_PATH=%CD%\output\pilot-runs\batch-01\learning.sqlite3"
echo Lingkao v0.2-alpha.3 pilot - batch-01
echo Open http://127.0.0.1:8010/ using a separate browser profile.
echo Read pilot\README.md before inviting students.
echo Local trial data: output\pilot-runs\batch-01\learning.sqlite3
call "app\start.bat" --port 8010
exit /b %ERRORLEVEL%
