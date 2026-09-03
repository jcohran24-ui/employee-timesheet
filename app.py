import os
import smtplib
from email.message import EmailMessage
from datetime import date, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, render_template, request, redirect, url_for, flash, session, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'change-me-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///timesheet.db').replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
TZ = ZoneInfo(os.getenv('APP_TIMEZONE', 'America/New_York'))


# Original single-user table retained for backward compatibility.
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
    __table_args__ = (UniqueConstraint('employee_name', 'work_date', name='uq_employee_work_date'),)


class TimesheetEmailSubmission(db.Model):
    __tablename__ = 'timesheet_email_submission'
    id = db.Column(db.Integer, primary_key=True)
    employee_name = db.Column(db.String(120), nullable=False, index=True)
    week_start = db.Column(db.Date, nullable=False, index=True)
    emailed_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint('employee_name', 'week_start', name='uq_employee_week_email'),)


class EmployeeAccount(db.Model):
    __tablename__ = 'employee_account'
    id = db.Column(db.Integer, primary_key=True)
    employee_name = db.Column(db.String(120), nullable=False)
    name_key = db.Column(db.String(120), nullable=False, unique=True, index=True)
    pin_hash = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class AdminAccount(db.Model):
    __tablename__ = 'admin_account'
    id = db.Column(db.Integer, primary_key=True)
    admin_name = db.Column(db.String(120), nullable=False)
    name_key = db.Column(db.String(120), nullable=False, unique=True, index=True)
    pin_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


def monday_for(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_days(start: date):
    return [start + timedelta(days=i) for i in range(7)]


def clean_name(value: str) -> str:
    return ' '.join((value or '').strip().split())[:120]


def name_key(value: str) -> str:
    return clean_name(value).casefold()


def valid_pin(pin: str) -> bool:
    return pin.isdigit() and 4 <= len(pin) <= 6


def employee_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        employee_id = session.get('employee_id')
        if not employee_id:
            return redirect(url_for('employee_login'))
        account = db.session.get(EmployeeAccount, employee_id)
        if not account or not account.active:
            session.pop('employee_id', None)
            flash('Please sign in.', 'warning')
            return redirect(url_for('employee_login'))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        admin_id = session.get('admin_id')
        if not admin_id or not db.session.get(AdminAccount, admin_id):
            return redirect(url_for('admin_login'))
        return view(*args, **kwargs)
    return wrapped


def current_employee():
    employee_id = session.get('employee_id')
    return db.session.get(EmployeeAccount, employee_id) if employee_id else None


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

    prior = TimesheetEmailSubmission.query.filter_by(employee_name=employee_name, week_start=week_start).first()
    if prior:
        return False, 'already_sent'

    subject, body = build_employee_email(employee_name, week_start)
    send_timesheet_email(subject, body)
    db.session.add(TimesheetEmailSubmission(employee_name=employee_name, week_start=week_start))
    db.session.commit()
    return True, None


def seed_admin_from_environment():
    admin_name = clean_name(os.getenv('ADMIN_NAME', ''))
    admin_pin = os.getenv('ADMIN_PIN', '').strip()
    if not admin_name or not valid_pin(admin_pin):
        return
    key = name_key(admin_name)
    if not AdminAccount.query.filter_by(name_key=key).first():
        db.session.add(AdminAccount(
            admin_name=admin_name,
            name_key=key,
            pin_hash=generate_password_hash(admin_pin),
        ))
        db.session.commit()


@app.before_request
def create_tables():
    db.create_all()
    seed_admin_from_environment()


@app.route('/employee', methods=['GET', 'POST'])
def employee_login():
    if request.method == 'POST':
        entered_name = clean_name(request.form.get('employee_name', ''))
        pin = (request.form.get('pin', '') or '').strip()
        account = EmployeeAccount.query.filter_by(name_key=name_key(entered_name), active=True).first()
        if not account or not check_password_hash(account.pin_hash, pin):
            flash('Name or PIN is incorrect.', 'danger')
            return redirect(url_for('employee_login'))
        session.clear()
        session['employee_id'] = account.id
        return redirect(url_for('index'))
    return render_template('employee.html')


@app.post('/logout')
def logout():
    session.clear()
    return redirect(url_for('employee_login'))


@app.route('/')
@employee_required
def index():
    account = current_employee()
    employee_name = account.employee_name

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
        rows.append({'date': d, 'regular': regular, 'overtime': overtime, 'notes': entry.notes if entry else ''})

    return render_template(
        'index.html', employee_name=employee_name, week_start=week_start, week_end=days[-1], rows=rows,
        total_regular=total_regular, total_overtime=total_overtime,
        grand_total=total_regular + total_overtime,
        prev_week=week_start - timedelta(days=7), next_week=week_start + timedelta(days=7),
    )


@app.post('/save')
@employee_required
def save():
    account = current_employee()
    employee_name = account.employee_name
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
                employee_name=employee_name, work_date=d,
                regular_hours=regular, overtime_hours=overtime, notes=notes,
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
    except Exception:
        app.logger.exception('Timesheet saved but Friday email failed')
        flash(f'{employee_name}\'s timesheet saved, but the email could not be sent. Please notify the supervisor.', 'warning')

    return redirect(url_for('index', week=week_start.isoformat()))


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        entered_name = clean_name(request.form.get('admin_name', ''))
        pin = (request.form.get('pin', '') or '').strip()
        account = AdminAccount.query.filter_by(name_key=name_key(entered_name)).first()
        if not account or not check_password_hash(account.pin_hash, pin):
            flash('Admin name or PIN is incorrect.', 'danger')
            return redirect(url_for('admin_login'))
        session.clear()
        session['admin_id'] = account.id
        return redirect(url_for('admin_dashboard'))
    return render_template('admin_login.html')


@app.post('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))


@app.get('/admin')
@admin_required
def admin_dashboard():
    employees = EmployeeAccount.query.order_by(EmployeeAccount.employee_name).all()
    return render_template('admin.html', employees=employees)


@app.post('/admin/employees/add')
@admin_required
def admin_add_employee():
    employee_name = clean_name(request.form.get('employee_name', ''))
    pin = (request.form.get('pin', '') or '').strip()
    if len(employee_name) < 2:
        flash('Enter the employee\'s full name.', 'danger')
    elif not valid_pin(pin):
        flash('PIN must be 4–6 digits.', 'danger')
    elif EmployeeAccount.query.filter_by(name_key=name_key(employee_name)).first():
        flash('That employee already has an account.', 'danger')
    else:
        db.session.add(EmployeeAccount(
            employee_name=employee_name,
            name_key=name_key(employee_name),
            pin_hash=generate_password_hash(pin),
            active=True,
        ))
        db.session.commit()
        flash(f'Account created for {employee_name}.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/employees/<int:employee_id>/toggle')
@admin_required
def admin_toggle_employee(employee_id):
    employee = db.session.get(EmployeeAccount, employee_id)
    if employee:
        employee.active = not employee.active
        db.session.commit()
        flash(f'{employee.employee_name} is now {"active" if employee.active else "inactive"}.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/employees/<int:employee_id>/delete')
@admin_required
def admin_delete_employee(employee_id):
    employee = db.session.get(EmployeeAccount, employee_id)
    if not employee:
        flash('Employee not found.', 'danger')
    else:
        employee_name = employee.employee_name
        db.session.delete(employee)
        db.session.commit()
        flash(f'{employee_name} login account deleted. Historical timesheets were kept.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/employees/<int:employee_id>/pin')
@admin_required
def admin_reset_pin(employee_id):
    employee = db.session.get(EmployeeAccount, employee_id)
    pin = (request.form.get('pin', '') or '').strip()
    if not employee:
        flash('Employee not found.', 'danger')
    elif not valid_pin(pin):
        flash('PIN must be 4–6 digits.', 'danger')
    else:
        employee.pin_hash = generate_password_hash(pin)
        db.session.commit()
        flash(f'PIN reset for {employee.employee_name}.', 'success')
    return redirect(url_for('admin_dashboard'))



@app.get('/manifest.webmanifest')
def manifest():
    response = send_from_directory(app.static_folder, 'manifest.webmanifest', mimetype='application/manifest+json')
    response.headers['Cache-Control'] = 'no-cache'
    return response


@app.get('/service-worker.js')
def service_worker():
    response = send_from_directory(app.static_folder, 'service-worker.js', mimetype='application/javascript')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['Service-Worker-Allowed'] = '/'
    return response

@app.get('/health')
def health():
    return {'status': 'ok'}, 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')), debug=os.getenv('FLASK_DEBUG') == '1')
