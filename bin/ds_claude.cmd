@echo off
setlocal
set PYTHONUTF8=1
python "%~dp0..\launch.py" ds %*
exit /b %errorlevel%
