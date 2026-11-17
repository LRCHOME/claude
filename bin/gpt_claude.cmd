@echo off
setlocal
node "%~dp0..\gpt-launch.mjs" %*
exit /b %errorlevel%
