import os
import smtplib
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from app import app, db, TimeEntry

TZ = ZoneInfo(os.getenv('APP_TIMEZONE', 'America/New_York'))


def monday_for(day: date) -> date:
    return day - timedelta(days=day.weekday())


def build_email(week_start: date):
    week_end = week_start + timedelta(days=6)
    entries = TimeEntry.query.filter(TimeEntry.work_date.between(week_start, week_end)).order_by(TimeEntry.work_date).all()
    by_date = {e.work_date: e for e in entries}

    lines = []
    total_regular = 0.0
    total_overtime = 0.0
    for i in range(7):
        d = week_start + timedelta(days=i)
        e = by_date.get(d)
        regular = e.regular_hours if e else 0.0
        overtime = e.overtime_hours if e else 0.0
        notes = e.notes if e and e.notes else ''
        total_regular += regular
        total_overtime += overtime
        lines.append(f'{d.strftime("%A %m/%d/%Y")}: Regular {regular:.2f} | OT {overtime:.2f}' + (f' | {notes}' if notes else ''))

    employee_name = os.getenv('EMPLOYEE_NAME', 'Employee')
    subject = f'Timesheet - {employee_name} - Week of {week_start.strftime("%m/%d/%Y")}'
    body = (
        f'{employee_name} weekly timesheet\n'
        f'Week: {week_start.strftime("%m/%d/%Y")} - {week_end.strftime("%m/%d/%Y")}\n\n'
        + '\n'.join(lines)
        + f'\n\nRegular Hours: {total_regular:.2f}'
        + f'\nOvertime Hours: {total_overtime:.2f}'
        + f'\nTotal Hours: {total_regular + total_overtime:.2f}\n'
    )
    return subject, body


def send_email(subject: str, body: str):
    host = os.environ['SMTP_HOST']
    port = int(os.getenv('SMTP_PORT', '587'))
    username = os.environ['SMTP_USERNAME']
    password = os.environ['SMTP_PASSWORD']
    from_email = os.getenv('SMTP_FROM', username)
    to_email = os.environ['TIMESHEET_TO_EMAIL']

    msg = EmailMessage()
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.set_content(body)

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(username, password)
        server.send_message(msg)


if __name__ == '__main__':
    today = datetime.now(TZ).date()
    # Friday email includes the current Monday-Friday entries, with Sat/Sun shown as zero unless entered.
    week_start = monday_for(today)
    with app.app_context():
        db.create_all()
        subject, body = build_email(week_start)
        send_email(subject, body)
        print('Weekly timesheet email sent.')
