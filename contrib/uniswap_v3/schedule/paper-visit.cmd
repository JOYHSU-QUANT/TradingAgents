@echo off
rem One visit of a contrib/uniswap_v3 paper run, for Windows Task Scheduler
rem (paper-visit.xml runs it three times a day). It appends the visit's
rem output to a log file and exits with the visit's exit code: 0 done
rem (decided now, or already decided), 1 something needs fixing, 3 try
rem again later. See contrib/uniswap_v3/RUNBOOK.md.
rem
rem Change the settings below, not the command. Paths are relative to this
rem file unless you make them absolute.
setlocal

set "RUN_ID=paper-1"
set "CONFIG=%~dp0..\configs\paper.local.yaml"
set "DB=%~dp0..\data\paper.db"
set "LOG=%~dp0..\data\paper-visits.log"
rem The Python that has contrib/uniswap_v3/requirements.txt installed. The
rem task runs with your PATH; to pin one, put its full path here:
rem   set "PYTHON=C:\Users\you\AppData\Local\Programs\Python\Python312\python.exe"
set "PYTHON=python"

rem The repository root, where the .env file with ETH_RPC_URL is.
cd /d "%~dp0..\..\.." || exit /b 1
for %%F in ("%LOG%") do if not exist "%%~dpF" mkdir "%%~dpF"
>>"%LOG%" echo ==== %DATE% %TIME% visit of %RUN_ID%
rem No --balance or --gas-eth: the run is started by hand once (RUNBOOK.md).
rem A visit to a run that is not there then fails with exit 1 instead of
rem quietly starting a new run under a mistyped --db or --run-id.
"%PYTHON%" -m dotenv run -- "%PYTHON%" -m contrib.uniswap_v3 paper --config "%CONFIG%" --db "%DB%" --run-id "%RUN_ID%" >>"%LOG%" 2>&1
set "CODE=%ERRORLEVEL%"
>>"%LOG%" echo ==== exit %CODE%
exit /b %CODE%
