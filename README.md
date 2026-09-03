# Weekly Timesheet App

A small Flask web application for entering daily regular and overtime hours, optional work notes, weekly totals, and a scheduled Friday email.

## Features
- Monday-Sunday weekly timesheet
- Regular hours and overtime hours
- Daily notes/work performed
- Live weekly totals
- Previous/current/next week navigation
- PostgreSQL support for deployment
- SQLite fallback for local testing
- Scheduled weekly email using SMTP
- Render web service + Render cron job configuration

## Local setup
1. Install Python 3.11+.
2. Create a virtual environment.
3. Run `pip install -r requirements.txt`.
4. Run `python app.py`.
5. Open `http://localhost:5000`.

For local testing, the app uses SQLite automatically if `DATABASE_URL` is not set.

## Email setup
The weekly email script reads these environment variables:
- `EMPLOYEE_NAME`
- `TIMESHEET_TO_EMAIL`
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_FROM`

For Gmail, use an App Password rather than your normal Gmail password.

## Render deployment
1. Push this project to GitHub.
2. Create a PostgreSQL database (Render Postgres or another provider such as Supabase).
3. Deploy using `render.yaml`, or manually create the web service and cron job.
4. Set `DATABASE_URL` to your PostgreSQL connection string on BOTH the web service and cron service.
5. Add the email environment variables to the cron service.
6. The included cron schedule is `0 21 * * 5`, which is 5:00 PM Eastern during daylight-saving time (21:00 UTC). Adjust for your preferred send time and daylight-saving changes.

## Important production note
This MVP is intended for a single employee/user. Before using it for multiple employees, add authentication and an employee/user ID to each time entry.
