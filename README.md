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

## Installable Android / iPhone PWA
This version is a Progressive Web App (PWA).

### Android
1. Open the Render URL in Chrome.
2. On the employee login page, tap **Install App** when shown.
3. The Timesheet icon will be added to the home screen/app launcher.

### iPhone
1. Open the Render URL in **Safari**.
2. Tap the **Share** button.
3. Tap **Add to Home Screen**.
4. Tap **Add**.

The app uses the same secure employee PIN login, database, admin dashboard, and Friday-email logic as the website. An internet connection is required to view/save live timesheets and send email.

## Admin-managed email recipients
The Admin dashboard now includes **Timesheet Email Recipients**. Add one or multiple addresses separated by commas, semicolons, or new lines. Saved recipients are stored in the database and used for both Friday-on-save emails and the weekly backup script. If no admin-managed recipients have been saved yet, the app falls back to the `TIMESHEET_TO_EMAIL` environment variable.


## Admin resend
The Admin dashboard includes **Resend Current Week** for each employee. It immediately emails that employee’s current-week timesheet to all configured recipients without deleting or changing the original Friday submission record.
