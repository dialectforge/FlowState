@echo off
rem FlowState v2 PUBLIC listener for the Cloudflare Tunnel (optional;
rem see docs/REMOTE-ACCESS.md "Go-live"). Listens on 127.0.0.1 only; cloudflared forwards
rem https://flowstate.example.com to it. Arguments go to `python -m flowstate`, e.g.
rem   --db <path-to>\flowstate.db --port 8767 --public-url https://flowstate.example.com
rem Log: %LOCALAPPDATA%\flowstate\logs\public-server.log (one line per request, no bodies).
setlocal
set "REPO=%~dp0..\.."
set "PYTHONPATH=%REPO%\mcp-server"
set "PYTHONUNBUFFERED=1"
set "LOGDIR=%LOCALAPPDATA%\flowstate\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
"%REPO%\.venv\Scripts\python.exe" -m flowstate --transport http --host 127.0.0.1 %* >> "%LOGDIR%\public-server.log" 2>&1
