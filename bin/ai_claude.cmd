@echo off
setlocal
set PYTHONUTF8=1
python "%~dp0..\launch.py" ai %*
exit /b %errorlevel%
