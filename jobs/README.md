# Scheduled jobs

Two daily Blinkit jobs. The suggestions run on the Lightsail server; the push
runs on the analytics office PC, because it signs in to Blinkit through the
alert mailbox.

| Time (IST) | Where | What runs |
|---|---|---|
| 12:30 pm | Lightsail server, ubuntu crontab (`0 7 * * *` UTC) | `oe.run_auto_campaigns()` in `/home/ubuntu/blinkit-marketing-repo/webapp` - the AI decides every keyword's bid for every campaign set to Auto. Log: `/home/ubuntu/ondemand-daily.log` |
| 1:15 pm | Lightsail server, ubuntu crontab (`45 7 * * *` UTC) | `perf_alerts.run()` in `/home/ubuntu/blinkit-marketing-repo/webapp` - flags non-Auto campaigns doing badly, asks the AI for pending suggestions, emails Amit (reminder when not accepted in 2 days). Log: `/home/ubuntu/perf-alerts.log` |
| 2:00 pm | Office PC, Task Scheduler `Blinkit_Ondemand_Bid_Push` | `Blinkit_Ondemand_Bid_Push.ipynb` - accepted changes are sent to Blinkit |

The server copy is the deploy checkout, so it is always on the latest tested
`main`. Which campaigns run comes only from the Auto switch in the dashboard
(`voylla.campaign_autonomy_mode`): never put campaign ids in the cron line.
Until 2026-10-13 it had two ids hard-coded, and campaigns switched to Auto
later were silently left out.

`Blinkit_Ondemand_Daily_Suggestions.ipynb` does the same as the cron line and
is kept for a manual run on the PC. Its Task Scheduler task is disabled - do
not enable it as well, or the suggestions run twice.

## How the PC push works

1. Task Scheduler starts the wrapper `.bat` in `Python_Scripts` (templates in `scheduler_wrappers/`).
2. The wrapper resets the job clone `C:\Users\Amit Singh\Documents\AI_Marketing_jobs` to the latest GitHub `main`, so a merged and tested change is used by the next run with no copying.
3. `run_job.bat` runs the notebook. The executed copy, with its output, goes to `Python_Scripts\logs\executed\`, never back into the repo.
4. If the run fails, an alert email is sent and the log is in `Python_Scripts\logs\`.

If GitHub can't be reached, the run still goes ahead with the code from the last successful update and the log says so.

## Rules

- Change these notebooks through a pull request like any other code. Never edit the files inside `AI_Marketing_jobs` by hand: every run resets it to `main`.
- Credentials are not in git. `webapp/db.py` reads them from `Python_Scripts` on the PC and `.env.production` on the server.
- Keep the notebooks free of outputs when committing (the tests check this).
