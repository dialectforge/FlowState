@echo off
rem FlowState v2 network (HTTP) server launcher (e.g. run by a logon scheduled task).
rem All arguments go to `python -m flowstate --transport http`, for example:
rem   --db <path-to>\flowstate.db --host <your-LAN-IP> --port 8766 --token-fallback-file <path-to>\http-token.txt
rem Token: FLOWSTATE_TOKEN, else the credential store, else the fallback file.
rem Log: %LOCALAPPDATA%\flowstate\logs\http-server.log (outside any synced folder).
setlocal
set "REPO=%~dp0..\.."
set "PYTHONPATH=%REPO%\mcp-server"
set "PYTHONUNBUFFERED=1"
set "LOGDIR=%LOCALAPPDATA%\flowstate\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
"%REPO%\.venv\Scripts\python.exe" -m flowstate --transport http %* >> "%LOGDIR%\http-server.log" 2>&1
