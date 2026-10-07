@echo off
rem ---------------------------------------------------------------------------
rem Runs one scheduled notebook from this repo's jobs\ folder.
rem   run_job.bat <notebook file name> <short log name>
rem Called by the Task Scheduler wrappers in Python_Scripts AFTER they have
rem pulled the latest main, so every run uses the merged, tested code.
rem The executed copy of the notebook (with its output) goes to the logs
rem folder, never back into the repo, so the checkout stays clean for the
rem next pull.
rem ---------------------------------------------------------------------------
setlocal

set "NOTEBOOK=%~1"
set "LOGNAME=%~2"
set "PYTHON=C:\Users\Amit Singh\AppData\Local\Programs\Python\Python313\python.exe"
set "SCRIPTS=C:\Users\Amit Singh\Documents\Python_Scripts"
set "ALERT_SCRIPT=%SCRIPTS%\send_failure_alert.py"
set "PYTHONIOENCODING=utf-8"
set "JOBS_DIR=%~dp0"

for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH-mm"') do set "STAMP=%%T"
set "LOGDIR=%SCRIPTS%\logs"
set "OUTDIR=%LOGDIR%\executed"
if not defined LOG set "LOG=%LOGDIR%\task_log_%LOGNAME%_%STAMP%.txt"
if not exist "%OUTDIR%" mkdir "%OUTDIR%"

echo Running %NOTEBOOK% from %JOBS_DIR% >> "%LOG%"
cd /d "%JOBS_DIR%" || (
    echo ERROR: cannot open jobs folder %JOBS_DIR% >> "%LOG%"
    exit /b 1
)

"%PYTHON%" -m jupyter nbconvert --execute --to notebook --ExecutePreprocessor.timeout=-1 --output-dir "%OUTDIR%" --output "%LOGNAME%_%STAMP%.ipynb" "%NOTEBOOK%" >> "%LOG%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"

echo Exit code: %EXIT_CODE% >> "%LOG%"
echo Finished: %DATE% %TIME% >> "%LOG%"
echo. >> "%LOG%"

if %EXIT_CODE% NEQ 0 (
    echo Sending failure alert... >> "%LOG%"
    "%PYTHON%" "%ALERT_SCRIPT%" "CRITICAL: %NOTEBOOK% FAILED (exit %EXIT_CODE%)" "The scheduled job %NOTEBOOK% failed with exit code %EXIT_CODE% on %DATE% %TIME%. Log: %LOG%" >> "%LOG%" 2>&1
)
exit /b %EXIT_CODE%
