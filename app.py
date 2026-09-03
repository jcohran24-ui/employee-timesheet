import os
import smtplib
from email.message import EmailMessage
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'change-me-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///timesheet.db').replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
TZ = ZoneInfo(os.getenv('APP_TIMEZONE', 'America/New_York'))


# Kept for compatibility with the original single-user database.
class TimeEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    work_date = db.Column(db.Date, nullable=False)
    regular_hours = db.Column(db.Float, nullable=False, default=0)
    overtime_hours = db.Column(db.Float, nullable=False, default=0)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint('work_date', name='uq_work_date'),)


class EmployeeTimeEntry(db.Model):
    __tablename__ = 'employee_time_entry'
    id = db.Column(db.Integer, primary_key=True)
    employee_name = db.Column(db.String(120), nullable=False, index=True)
    work_date = db.Column(db.Date, nullable=False, index=True)
    regular_hours = db.Column(db.Float, nullable=False, default=0)
    overtime_hours = db.Column(db.Float, nullable=False, default=0)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint('employee_name', 'work_date', name='uq_employee_work_date'),
    )



class TimesheetEmailSubmission(db.Model):
    __tablename__ = 'timesheet_email_submission'
    id = db.Column(db.Integer, primary_key=True)
    employee_name = db.Column(db.String(120), nullable=False, index=True)
    week_start = db.Column(db.Date, nullable=False, index=True)
    emailed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint('employee_name', 'week_start', name='uq_employee_week_email'),
    )

def monday_for(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_days(start: date):
    return [start + timedelta(days=i) for i in range(7)]


def clean_employee_name(value: str) -> str:
    return ' '.join((value or '').strip().split())[:120]



def build_employee_email(employee_name: str, week_start: date):
    week_end = week_start + timedelta(days=6)
    entries = EmployeeTimeEntry.query.filter(
        EmployeeTimeEntry.employee_name == employee_name,
        EmployeeTimeEntry.work_date.between(week_start, week_end)
    ).order_by(EmployeeTimeEntry.work_date).all()
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
        lines.append(
            f'{d.strftime("%A %m/%d/%Y")}: Regular {regular:.2f} | OT {overtime:.2f}'
            + (f' | {notes}' if notes else '')
        )

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


def send_timesheet_email(subject: str, body: str):
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


def email_if_friday(employee_name: str, week_start: date):
    today = datetime.now(TZ).date()
    if today.weekday() != 4 or monday_for(today) != week_start:
        return False, None

    prior = TimesheetEmailSubmission.query.filter_by(
        employee_name=employee_name, week_start=week_start
    ).first()
    if prior:
        return False, 'already_sent'

    subject, body = build_employee_email(employee_name, week_start)
    send_timesheet_email(subject, body)
    db.session.add(TimesheetEmailSubmission(
        employee_name=employee_name, week_start=week_start
    ))
    db.session.commit()
    return True, None

@app.before_request
def create_tables():
    db.create_all()


@app.route('/employee', methods=['GET', 'POST'])
def employee():
    if request.method == 'POST':
        name = clean_employee_name(request.form.get('employee_name', ''))
        if len(name) < 2:
            flash('Please enter your full name.', 'danger')
            return redirect(url_for('employee'))
        session['employee_name'] = name
        return redirect(url_for('index'))
    return render_template('employee.html', employee_name=session.get('employee_name', ''))


@app.post('/switch-employee')
def switch_employee():
    session.pop('employee_name', None)
    return redirect(url_for('employee'))


@app.route('/')
def index():
    employee_name = clean_employee_name(session.get('employee_name', ''))
    if not employee_name:
        return redirect(url_for('employee'))

    requested = request.args.get('week')
    try:
        base = date.fromisoformat(requested) if requested else datetime.now(TZ).date()
    except ValueError:
        base = datetime.now(TZ).date()

    week_start = monday_for(base)
    days = week_days(week_start)
    entries = EmployeeTimeEntry.query.filter(
        EmployeeTimeEntry.employee_name == employee_name,
        EmployeeTimeEntry.work_date.between(days[0], days[-1])
    ).all()
    by_date = {e.work_date: e for e in entries}

    rows = []
    total_regular = 0.0
    total_overtime = 0.0
    for d in days:
        entry = by_date.get(d)
        regular = entry.regular_hours if entry else 0.0
        overtime = entry.overtime_hours if entry else 0.0
        total_regular += regular
        total_overtime += overtime
        rows.append({
            'date': d,
            'regular': regular,
            'overtime': overtime,
            'notes': entry.notes if entry else '',
        })

    return render_template(
        'index.html',
        employee_name=employee_name,
        week_start=week_start,
        week_end=days[-1],
        rows=rows,
        total_regular=total_regular,
        total_overtime=total_overtime,
        grand_total=total_regular + total_overtime,
        prev_week=week_start - timedelta(days=7),
        next_week=week_start + timedelta(days=7),
    )


@app.post('/save')
def save():
    employee_name = clean_employee_name(session.get('employee_name', ''))
    if not employee_name:
        return redirect(url_for('employee'))

    week_start = date.fromisoformat(request.form['week_start'])
    for d in week_days(week_start):
        key = d.isoformat()
        try:
            regular = float(request.form.get(f'regular_{key}', 0) or 0)
            overtime = float(request.form.get(f'overtime_{key}', 0) or 0)
        except ValueError:
            flash(f'Invalid hours for {d.strftime("%A, %b %d")}.', 'danger')
            return redirect(url_for('index', week=week_start.isoformat()))

        notes = (request.form.get(f'notes_{key}', '') or '').strip()

        if regular < 0 or overtime < 0 or regular > 24 or overtime > 24 or regular + overtime > 24:
            flash(f'Invalid hours for {d.strftime("%A, %b %d")}. Daily total must be between 0 and 24.', 'danger')
            return redirect(url_for('index', week=week_start.isoformat()))

        entry = EmployeeTimeEntry.query.filter_by(employee_name=employee_name, work_date=d).first()
        if entry:
            entry.regular_hours = regular
            entry.overtime_hours = overtime
            entry.notes = notes
        elif regular or overtime or notes:
            db.session.add(EmployeeTimeEntry(
                employee_name=employee_name,
                work_date=d,
                regular_hours=regular,
                overtime_hours=overtime,
                notes=notes,
            ))

    db.session.commit()

    try:
        sent, reason = email_if_friday(employee_name, week_start)
        if sent:
            flash(f'{employee_name}\'s timesheet saved and emailed successfully.', 'success')
        elif reason == 'already_sent':
            flash(f'{employee_name}\'s timesheet saved. This week\'s Friday email was already sent.', 'success')
        else:
            flash(f'{employee_name}\'s timesheet saved.', 'success')
    except Exception as exc:
        app.logger.exception('Timesheet saved but Friday email failed')
        flash(f'{employee_name}\'s timesheet saved, but the email could not be sent. Please notify the supervisor.', 'warning')

    return redirect(url_for('index', week=week_start.isoformat()))


@app.get('/health')
def health():
    return {'status': 'ok'}, 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')), debug=os.getenv('FLASK_DEBUG') == '1')
