@echo off
setlocal
set PYTHONUTF8=1
python "%~dp0..\desktop_gateway.py" %*
exit /b %errorlevel%
