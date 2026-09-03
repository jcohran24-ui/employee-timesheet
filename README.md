# Secure Employee Timesheet

Multi-employee Flask timesheet with employee PIN logins, supervisor/admin management, separate employee timesheets, and Friday email on save.

## New Render environment variables

Add these to the **web service** Environment section:

- `ADMIN_NAME` — your supervisor/admin login name
- `ADMIN_PIN` — a 4–6 digit PIN used to create the first admin account

Existing required variables remain:

- `DATABASE_URL`
- `SECRET_KEY`
- `APP_TIMEZONE=America/New_York`
- `SMTP_HOST=smtp.gmail.com`
- `SMTP_PORT=587`
- `SMTP_USERNAME`
- `SMTP_PASSWORD` (Google App Password)
- `SMTP_FROM`
- `TIMESHEET_TO_EMAIL`

## Important

The admin PIN is hashed in the database when the admin account is first created. Changing `ADMIN_PIN` later does not automatically change the existing admin PIN.

Employee PINs are also stored as password hashes, not plain text.

Deactivating an employee blocks access but preserves historical timesheets.


## Delete User
The Admin dashboard includes **Delete User**. This permanently removes the employee login account only. Historical `employee_time_entry` records and prior `timesheet_email_submission` records are intentionally retained.
