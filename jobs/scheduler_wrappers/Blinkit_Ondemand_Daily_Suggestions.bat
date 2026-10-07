@echo off
rem ---------------------------------------------------------------------------
rem Scheduled by Windows Task Scheduler (12:30 pm IST).
rem 1. Brings the job copy of AI_Marketing up to date with GitHub main.
rem 2. Runs jobs\Blinkit_Ondemand_Daily_Suggestions.ipynb from it (jobs\run_job.bat).
rem Template kept in git: jobs\scheduler_wrappers\Blinkit_Ondemand_Daily_Suggestions.bat
rem Do not edit files inside %REPO% by hand - every run resets it to main.
rem ---------------------------------------------------------------------------
setlocal
set "GIT=C:\Users\Amit Singh\AppData\Local\Programs\Git\cmd\git.exe"
set "REPO=C:\Users\Amit Singh\Documents\AI_Marketing_jobs"
set "LOGDIR=C:\Users\Amit Singh\Documents\Python_Scripts\logs"
for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH-mm"') do set "STAMP=%%T"
set "LOG=%LOGDIR%\task_log_ondemand_daily_%STAMP%.txt"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo =============================== > "%LOG%"
echo Run started: %DATE% %TIME% >> "%LOG%"
echo =============================== >> "%LOG%"

"%GIT%" -C "%REPO%" fetch --quiet origin main >> "%LOG%" 2>&1
if errorlevel 1 (
    echo WARNING: could not reach GitHub - running the code from the last successful update. >> "%LOG%"
) else (
    "%GIT%" -C "%REPO%" reset --quiet --hard origin/main >> "%LOG%" 2>&1
)
for /f %%C in ('"%GIT%" -C "%REPO%" rev-parse --short HEAD') do echo Code version: %%C >> "%LOG%"

call "%REPO%\jobs\run_job.bat" "Blinkit_Ondemand_Daily_Suggestions.ipynb" "ondemand_daily"
exit /b %ERRORLEVEL%
