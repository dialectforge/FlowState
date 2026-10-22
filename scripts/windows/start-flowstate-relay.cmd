@echo off
rem FlowState relay agent (fsmcp.com). Runs FlowState's public listener on 127.0.0.1:8768
rem and keeps the encrypted tunnel to the relay open. Uses this checkout's .venv.
rem All arguments go to `python -m flowstate relay run`, for example: --db <path-to>\flowstate.db
rem Log: %LOCALAPPDATA%\flowstate\logs\relay-agent.log (no request bodies, no tokens).
setlocal
set "REPO=%~dp0..\.."
set "PYTHONPATH=%REPO%\mcp-server"
set "PYTHONUNBUFFERED=1"
set "LOGDIR=%LOCALAPPDATA%\flowstate\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
"%REPO%\.venv\Scripts\python.exe" -m flowstate relay run %* >> "%LOGDIR%\relay-agent.log" 2>&1