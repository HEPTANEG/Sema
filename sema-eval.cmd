@echo off
if "%SEMA_COMMAND%"=="" set "SEMA_COMMAND=%~dp0sema.cmd"
python "%~dp0sema_eval\main.py" %*
