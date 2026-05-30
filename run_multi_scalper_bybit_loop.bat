@echo off
REM ===========================================================================
REM  Bybit Multi-Symbol Scalper — LIVE LOOP MODE
REM
REM  Runs app/run_multi_scalper_bybit.py forever, restarting on crash with
REM  a 60s backoff. Per-symbol winner configs from
REM  notebooks/results/_top_per_symbol/<SYM>/config.json.
REM
REM  WARNING:
REM      Without --dry-run, market orders ARE sent to Bybit.
REM      Edit RUN_ARGS below to set --dry-run, --testnet, or --demo before
REM      using a real account.
REM
REM  Usage:
REM      run_multi_scalper_bybit_loop.bat
REM
REM  Behaviour:
REM      The Python runner sleeps internally until the next M5 close, then
REM      evaluates every profitable winner symbol once. Open positions stay
REM      open after Ctrl+C — they continue under their broker-side SL/TP
REM      until you relaunch the bot or close manually.
REM
REM  Logs:
REM      Per-symbol JSON  -> logs\bybit_bot\<SYMBOL>-YYYY-MM-DD.jsonl
REM      Portfolio JSON   -> logs\bybit_bot\_portfolio.jsonl
REM      Wrapper stdout   -> logs\bybit_bot_loop\loop-YYYYMMDD-HHMMSS.log
REM
REM  Stop:
REM      Ctrl+C in this window. The Python runner emits a bot_run_stopped
REM      event and a Telegram message before exiting.
REM ===========================================================================

setlocal

REM ---- adjust to your local checkout root ----
set REPO_ROOT=%~dp0
set REPO_ROOT=%REPO_ROOT:~0,-1%

REM Prefer the venv interpreter when present; fall back to system python.
set VENV_PY=%REPO_ROOT%\.venv\Scripts\python.exe
if exist "%VENV_PY%" (
    set PYTHON=%VENV_PY%
) else (
    set PYTHON=python
)

REM ---- runtime flags (edit before launch) ----
REM   --dry-run       : detect + log + notify, do not place orders
REM   --testnet       : connect to api-testnet.bybit.com (paper)
REM   --demo          : connect to api-demo.bybit.com   (paper)
REM   --risk-usdt 10  : per-trade risk in USDT (default 20)
REM   --symbols X Y Z : restrict basket
set RUN_ARGS=

REM ---- per-launch log directory ----
set LOOP_LOG_DIR=%REPO_ROOT%\logs\bybit_bot_loop
if not exist "%LOOP_LOG_DIR%" mkdir "%LOOP_LOG_DIR%"

for /f "delims=" %%I in ('powershell -NoLogo -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set RUN_TS=%%I
set LOOP_LOG=%LOOP_LOG_DIR%\loop-%RUN_TS%.log

echo ========================================================================
echo  Bybit Multi-Symbol Scalper — LIVE LOOP
echo  Started: %date% %time%
echo  Repo:    %REPO_ROOT%
echo  Python:  %PYTHON%
echo  Logs:    %LOOP_LOG%
echo  Args:    %RUN_ARGS%
echo  Stop:    Ctrl+C  (clean shutdown; open positions stay open)
echo ========================================================================

cd /d "%REPO_ROOT%"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PYTHONPATH=%REPO_ROOT%

:loop
echo [%date% %time%] launching python runner...
echo [%date% %time%] launching python runner >> "%LOOP_LOG%"

"%PYTHON%" -u app\run_multi_scalper_bybit.py %RUN_ARGS% >> "%LOOP_LOG%" 2>&1
set EXIT_CODE=%ERRORLEVEL%

echo [%date% %time%] python exited with code %EXIT_CODE%
echo [%date% %time%] python exited with code %EXIT_CODE% >> "%LOOP_LOG%"

echo [%date% %time%] sleeping 60s before restart (Ctrl+C to abort)...
echo [%date% %time%] sleeping 60s before restart >> "%LOOP_LOG%"
timeout /t 60 /nobreak >nul

goto loop

endlocal
