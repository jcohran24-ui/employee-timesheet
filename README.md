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

## Automatic overtime calculation
Employees enter only total hours worked per day. The application automatically assigns the first 40 hours in each Monday-Sunday workweek to regular time and all hours above 40 to overtime. The same stored regular/overtime split is used in Friday emails and admin resends.


## First-login PIN change
Employee PINs created by an admin are temporary. On the employee's next login, the app requires a new 4–6 digit PIN before the timesheet can be opened. Admin PIN resets also force this change. Existing employee accounts are prompted once after this upgrade.

Add these environment variables to the Render Web Service:

- `APP_BASE_URL` — optional but recommended public app URL, e.g. `https://employee-timesheet.onrender.com`


Existing databases are upgraded automatically with a nullable `phone_number` column on `employee_account`.


## Public policy pages

After deployment, these public URLs are available without login:

- `/privacy` — Privacy Policy
- `/terms` — Terms of Use

`https://your-app.onrender.com/privacy`


## Admin employee timesheet view
Employee names on the Admin dashboard are clickable. Admins can review current and historical weekly timesheets, totals, notes, and email status without signing in as the employee.

## Admin employee timesheet upgrades

The employee detail page now allows an authenticated admin to edit each day's total hours and notes. The application automatically recalculates the first 40 weekly hours as regular time and hours above 40 as overtime.

Admins can also download the selected employee/week as a PDF from the employee detail page. Admin edits do not automatically resend previously emailed timesheets; use the existing resend control when a corrected current-week email is needed.


## Mobile-first layout
The employee timesheet, admin dashboard, and admin employee detail page automatically switch to stacked card layouts on phone-size screens. Buttons and inputs use larger touch targets, and the installed PWA continues to work on Android and iPhone.


## Edit employee profiles

## Admin dashboard upgrades
- Summary cards for Active Employees, Timesheets This Week, Missing Timesheets, and Hours This Week.
- Employee search and sorting.
- Combined weekly PDF and CSV exports.
- Admin-editable company/app name, email recipients, overtime threshold, timezone, and weekly cutoff time.

## Selected bulk reports
The Admin weekly report section now lets the administrator choose exactly which employees are included in the combined PDF or CSV export. Active employees are selected by default, with Select All and Clear controls.

The Admin summary bar is reduced to two compact metrics: Active Employees and Hours This Week.

## Optional job information on PDF
Admins can optionally add work-order/ticket information before downloading an employee PDF. The fields are prefilled with the sample ticket values and can be edited or cleared. They apply only to that PDF download and do not alter the saved timesheet.
