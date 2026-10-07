# Scheduled jobs

The two daily Blinkit jobs run on the analytics office PC, from a clone of
this repo at `C:\Users\Amit Singh\Documents\AI_Marketing_jobs`.

| Time (IST) | Task Scheduler task | Notebook |
|---|---|---|
| 12:30 pm | Blinkit_Ondemand_Daily_Suggestions | `Blinkit_Ondemand_Daily_Suggestions.ipynb` - the AI decides every keyword's bid |
| 1:00 pm | Blinkit_Ondemand_Bid_Push | `Blinkit_Ondemand_Bid_Push.ipynb` - accepted changes are sent to Blinkit |

## How a run works

1. Task Scheduler starts the wrapper `.bat` in `Python_Scripts` (templates in `scheduler_wrappers/`).
2. The wrapper resets the job clone to the latest GitHub `main`, so a merged and tested change is used by the next run with no copying.
3. `run_job.bat` runs the notebook. The executed copy, with its output, goes to `Python_Scripts\logs\executed\`, never back into the repo.
4. If the run fails, an alert email is sent and the log is in `Python_Scripts\logs\`.

If GitHub can't be reached, the run still goes ahead with the code from the last successful update and the log says so.

## Rules

- Change these notebooks through a pull request like any other code. Never edit the files inside `AI_Marketing_jobs` by hand: every run resets it to `main`.
- Credentials are not in git. `webapp/db.py` reads them from `Python_Scripts`.
- Keep the notebooks free of outputs when committing (the tests check this).
