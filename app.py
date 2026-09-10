import os
import smtplib
import base64
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import parseaddr
from datetime import date, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, render_template, request, redirect, url_for, flash, session, send_from_directory, send_file
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint, inspect, text
from werkzeug.security import generate_password_hash, check_password_hash
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

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
    must_change_pin = db.Column(db.Boolean, nullable=False, default=True)
    phone_number = db.Column(db.String(20), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)




class AppSetting(db.Model):
    __tablename__ = 'app_setting'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), nullable=False, unique=True, index=True)
    value = db.Column(db.Text, nullable=False, default='')
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


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


def normalize_phone(value: str) -> str:
    raw = (value or '').strip()
    digits = ''.join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10:
        return '+1' + digits
    if len(digits) == 11 and digits.startswith('1'):
        return '+' + digits
    if raw.startswith('+') and 8 <= len(digits) <= 15:
        return '+' + digits
    return ''


def app_login_url() -> str:
    configured = os.getenv('APP_BASE_URL', '').strip().rstrip('/')
    if configured:
        return configured + '/employee'
    return request.url_root.rstrip('/') + url_for('employee_login')


def send_twilio_sms(to_number: str, message: str):
    account_sid = os.environ['TWILIO_ACCOUNT_SID']
    auth_token = os.environ['TWILIO_AUTH_TOKEN']
    from_number = os.environ['TWILIO_PHONE_NUMBER']
    endpoint = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json'
    payload = urllib.parse.urlencode({
        'To': to_number,
        'From': from_number,
        'Body': message,
    }).encode('utf-8')
    credentials = base64.b64encode(f'{account_sid}:{auth_token}'.encode()).decode()
    req = urllib.request.Request(endpoint, data=payload, method='POST')
    req.add_header('Authorization', f'Basic {credentials}')
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')
    with urllib.request.urlopen(req, timeout=15) as response:
        if response.status not in (200, 201):
            raise RuntimeError(f'Twilio returned HTTP {response.status}')


def send_login_text(employee_name: str, phone_number: str, temporary_pin: str):
    message = (
        'JC Timesheet\n'
        f'Login: {employee_name}\n'
        f'Temporary PIN: {temporary_pin}\n'
        f'Open: {app_login_url()}\n'
        'You will be required to create a new PIN after signing in.'
    )
    send_twilio_sms(phone_number, message)


def valid_email(value: str) -> bool:
    value = (value or '').strip()
    _name, addr = parseaddr(value)
    return bool(addr and addr == value and '@' in addr and '.' in addr.rsplit('@', 1)[-1])


def get_timesheet_recipients():
    setting = AppSetting.query.filter_by(key='timesheet_recipients').first()
    if setting and setting.value.strip():
        return [x.strip() for x in setting.value.split(',') if x.strip()]
    fallback = os.getenv('TIMESHEET_TO_EMAIL', '')
    return [x.strip() for x in fallback.replace(';', ',').split(',') if x.strip()]


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
        if account.must_change_pin and request.endpoint != 'employee_change_pin':
            return redirect(url_for('employee_change_pin'))
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


def get_employee_week_rows(employee_name: str, week_start: date):
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
        total = regular + overtime
        total_regular += regular
        total_overtime += overtime
        rows.append({
            'date': d, 'regular': regular, 'overtime': overtime, 'total': total,
            'notes': entry.notes if entry else ''
        })
    return rows, total_regular, total_overtime


def build_timesheet_pdf(employee_name: str, week_start: date):
    rows, total_regular, total_overtime = get_employee_week_rows(employee_name, week_start)
    week_end = week_start + timedelta(days=6)
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36,
        title=f'{employee_name} Timesheet'
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'TimesheetTitle', parent=styles['Title'], alignment=TA_CENTER, fontSize=18, leading=22, spaceAfter=6
    )
    subtitle_style = ParagraphStyle(
        'TimesheetSubtitle', parent=styles['Normal'], alignment=TA_CENTER, fontSize=10, textColor=colors.HexColor('#555555'), spaceAfter=14
    )
    story = [
        Paragraph('Employee Timesheet', title_style),
        Paragraph(employee_name, styles['Heading2']),
        Paragraph(
            f'Week of {week_start.strftime("%m/%d/%Y")} - {week_end.strftime("%m/%d/%Y")}',
            subtitle_style
        ),
        Spacer(1, 6),
    ]
    data = [['Day', 'Date', 'Total', 'Regular', 'OT', 'Notes']]
    for row in rows:
        notes = (row['notes'] or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        data.append([
            row['date'].strftime('%A'),
            row['date'].strftime('%m/%d/%Y'),
            f"{row['total']:.2f}",
            f"{row['regular']:.2f}",
            f"{row['overtime']:.2f}",
            Paragraph(notes or '-', styles['BodyText']),
        ])
    data.append(['Weekly Totals', '', f'{total_regular + total_overtime:.2f}', f'{total_regular:.2f}', f'{total_overtime:.2f}', ''])
    table = Table(data, colWidths=[72, 72, 48, 52, 42, 210], repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#0d6efd')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('ALIGN', (2,1), (4,-1), 'RIGHT'),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#cccccc')),
        ('ROWBACKGROUNDS', (0,1), (-1,-2), [colors.white, colors.HexColor('#f7f9fc')]),
        ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor('#e9ecef')),
        ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
    ]))
    story.append(table)
    story.append(Spacer(1, 12))
    doc.build(story)
    buffer.seek(0)
    return buffer


def send_timesheet_email(subject: str, body: str):
    host = os.environ['SMTP_HOST']
    port = int(os.getenv('SMTP_PORT', '587'))
    username = os.environ['SMTP_USERNAME']
    password = os.environ['SMTP_PASSWORD']
    from_email = os.getenv('SMTP_FROM', username)
    recipients = get_timesheet_recipients()
    if not recipients:
        raise RuntimeError('No timesheet email recipients are configured.')

    msg = EmailMessage()
    msg['From'] = from_email
    msg['To'] = ', '.join(recipients)
    msg['Subject'] = subject
    msg.set_content(body)

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(username, password)
        server.send_message(msg, to_addrs=recipients)


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


def ensure_employee_account_columns():
    # db.create_all() does not add columns to an existing table, so perform
    # small backward-compatible migrations automatically on deployment.
    columns = {column['name'] for column in inspect(db.engine).get_columns('employee_account')}
    if 'must_change_pin' not in columns:
        db.session.execute(text(
            'ALTER TABLE employee_account '
            'ADD COLUMN must_change_pin BOOLEAN NOT NULL DEFAULT TRUE'
        ))
        db.session.commit()
    columns = {column['name'] for column in inspect(db.engine).get_columns('employee_account')}
    if 'phone_number' not in columns:
        db.session.execute(text(
            'ALTER TABLE employee_account '
            'ADD COLUMN phone_number VARCHAR(20)'
        ))
        db.session.commit()


@app.before_request
def create_tables():
    db.create_all()
    ensure_employee_account_columns()
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
        if account.must_change_pin:
            return redirect(url_for('employee_change_pin'))
        return redirect(url_for('index'))
    return render_template('employee.html')


@app.route('/change-pin', methods=['GET', 'POST'])
def employee_change_pin():
    employee_id = session.get('employee_id')
    account = db.session.get(EmployeeAccount, employee_id) if employee_id else None
    if not account or not account.active:
        session.clear()
        flash('Please sign in.', 'warning')
        return redirect(url_for('employee_login'))

    if request.method == 'POST':
        new_pin = (request.form.get('new_pin', '') or '').strip()
        confirm_pin = (request.form.get('confirm_pin', '') or '').strip()
        if not valid_pin(new_pin):
            flash('Your new PIN must be 4–6 digits.', 'danger')
        elif new_pin != confirm_pin:
            flash('The PINs do not match.', 'danger')
        elif check_password_hash(account.pin_hash, new_pin):
            flash('Choose a PIN different from your temporary PIN.', 'danger')
        else:
            account.pin_hash = generate_password_hash(new_pin)
            account.must_change_pin = False
            db.session.commit()
            flash('Your PIN has been changed successfully.', 'success')
            return redirect(url_for('index'))

    return render_template('change_pin.html', employee_name=account.employee_name)


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
        daily_total = regular + overtime
        total_regular += regular
        total_overtime += overtime
        rows.append({
            'date': d,
            'total': daily_total,
            'regular': regular,
            'overtime': overtime,
            'notes': entry.notes if entry else ''
        })

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

    weekly_regular_used = 0.0
    for d in week_days(week_start):
        key = d.isoformat()
        try:
            daily_total = float(request.form.get(f'total_{key}', 0) or 0)
        except ValueError:
            flash(f'Invalid hours for {d.strftime("%A, %b %d")}.', 'danger')
            return redirect(url_for('index', week=week_start.isoformat()))

        notes = (request.form.get(f'notes_{key}', '') or '').strip()
        if daily_total < 0 or daily_total > 24:
            flash(f'Invalid hours for {d.strftime("%A, %b %d")}. Daily total must be between 0 and 24.', 'danger')
            return redirect(url_for('index', week=week_start.isoformat()))

        regular_available = max(0.0, 40.0 - weekly_regular_used)
        regular = min(daily_total, regular_available)
        overtime = max(0.0, daily_total - regular)
        weekly_regular_used += regular

        entry = EmployeeTimeEntry.query.filter_by(employee_name=employee_name, work_date=d).first()
        if entry:
            entry.regular_hours = regular
            entry.overtime_hours = overtime
            entry.notes = notes
        elif daily_total or notes:
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
    recipients = get_timesheet_recipients()
    return render_template('admin.html', employees=employees, recipients=recipients)



@app.get('/admin/employee/<int:employee_id>/timesheet')
@admin_required
def admin_employee_timesheet(employee_id):
    employee = db.session.get(EmployeeAccount, employee_id)
    if not employee:
        flash('Employee not found.', 'danger')
        return redirect(url_for('admin_dashboard'))

    requested = request.args.get('week')
    try:
        base_day = date.fromisoformat(requested) if requested else datetime.now(TZ).date()
    except ValueError:
        base_day = datetime.now(TZ).date()

    week_start = monday_for(base_day)
    days = week_days(week_start)
    rows, total_regular, total_overtime = get_employee_week_rows(employee.employee_name, week_start)

    submission = TimesheetEmailSubmission.query.filter_by(
        employee_name=employee.employee_name, week_start=week_start
    ).first()

    return render_template(
        'admin_employee_timesheet.html',
        employee=employee,
        week_start=week_start,
        week_end=days[-1],
        rows=rows,
        total_regular=total_regular,
        total_overtime=total_overtime,
        grand_total=total_regular + total_overtime,
        prev_week=week_start - timedelta(days=7),
        next_week=week_start + timedelta(days=7),
        submission=submission,
    )

@app.post('/admin/employee/<int:employee_id>/timesheet/save')
@admin_required
def admin_save_employee_timesheet(employee_id):
    employee = db.session.get(EmployeeAccount, employee_id)
    if not employee:
        flash('Employee not found.', 'danger')
        return redirect(url_for('admin_dashboard'))

    try:
        week_start = date.fromisoformat(request.form['week_start'])
    except (KeyError, ValueError):
        flash('Invalid timesheet week.', 'danger')
        return redirect(url_for('admin_employee_timesheet', employee_id=employee_id))

    weekly_regular_used = 0.0
    for d in week_days(week_start):
        key = d.isoformat()
        try:
            daily_total = float(request.form.get(f'total_{key}', 0) or 0)
        except ValueError:
            flash(f'Invalid hours for {d.strftime("%A, %b %d")}.', 'danger')
            return redirect(url_for('admin_employee_timesheet', employee_id=employee_id, week=week_start.isoformat()))

        notes = (request.form.get(f'notes_{key}', '') or '').strip()
        if daily_total < 0 or daily_total > 24:
            flash(f'Invalid hours for {d.strftime("%A, %b %d")}. Daily total must be between 0 and 24.', 'danger')
            return redirect(url_for('admin_employee_timesheet', employee_id=employee_id, week=week_start.isoformat()))

        regular_available = max(0.0, 40.0 - weekly_regular_used)
        regular = min(daily_total, regular_available)
        overtime = max(0.0, daily_total - regular)
        weekly_regular_used += regular

        entry = EmployeeTimeEntry.query.filter_by(employee_name=employee.employee_name, work_date=d).first()
        if entry:
            if daily_total or notes:
                entry.regular_hours = regular
                entry.overtime_hours = overtime
                entry.notes = notes
            else:
                db.session.delete(entry)
        elif daily_total or notes:
            db.session.add(EmployeeTimeEntry(
                employee_name=employee.employee_name,
                work_date=d,
                regular_hours=regular,
                overtime_hours=overtime,
                notes=notes,
            ))

    db.session.commit()
    flash(f'{employee.employee_name}\'s timesheet was updated by Admin. Regular and overtime totals were recalculated.', 'success')
    return redirect(url_for('admin_employee_timesheet', employee_id=employee_id, week=week_start.isoformat()))


@app.get('/admin/employee/<int:employee_id>/timesheet/pdf')
@admin_required
def admin_employee_timesheet_pdf(employee_id):
    employee = db.session.get(EmployeeAccount, employee_id)
    if not employee:
        flash('Employee not found.', 'danger')
        return redirect(url_for('admin_dashboard'))

    requested = request.args.get('week')
    try:
        base_day = date.fromisoformat(requested) if requested else datetime.now(TZ).date()
    except ValueError:
        base_day = datetime.now(TZ).date()
    week_start = monday_for(base_day)
    pdf = build_timesheet_pdf(employee.employee_name, week_start)
    safe_name = ''.join(ch if ch.isalnum() else '_' for ch in employee.employee_name).strip('_') or 'employee'
    filename = f'{safe_name}_timesheet_{week_start.isoformat()}.pdf'
    return send_file(pdf, mimetype='application/pdf', as_attachment=True, download_name=filename)


@app.post('/admin/email-recipients')
@admin_required
def admin_email_recipients():
    raw = request.form.get('recipients', '') or ''
    parts = [x.strip() for x in raw.replace(';', ',').replace('\n', ',').split(',') if x.strip()]
    recipients = []
    seen = set()
    invalid = []
    for address in parts:
        if not valid_email(address):
            invalid.append(address)
            continue
        key = address.casefold()
        if key not in seen:
            seen.add(key)
            recipients.append(address)

    if invalid:
        flash('These email addresses are invalid: ' + ', '.join(invalid), 'danger')
        return redirect(url_for('admin_dashboard'))
    if not recipients:
        flash('Enter at least one valid email address.', 'danger')
        return redirect(url_for('admin_dashboard'))

    setting = AppSetting.query.filter_by(key='timesheet_recipients').first()
    if not setting:
        setting = AppSetting(key='timesheet_recipients')
        db.session.add(setting)
    setting.value = ','.join(recipients)
    db.session.commit()
    suffix = 'es' if len(recipients) != 1 else ''
    flash(f'Email recipients updated ({len(recipients)} address{suffix}).', 'success')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/employees/add')
@admin_required
def admin_add_employee():
    employee_name = clean_name(request.form.get('employee_name', ''))
    pin = (request.form.get('pin', '') or '').strip()
    phone_input = (request.form.get('phone_number', '') or '').strip()
    phone_number = normalize_phone(phone_input) if phone_input else ''
    send_text = request.form.get('send_welcome_text') == '1'

    if len(employee_name) < 2:
        flash('Enter the employee\'s full name.', 'danger')
    elif not valid_pin(pin):
        flash('PIN must be 4–6 digits.', 'danger')
    elif phone_input and not phone_number:
        flash('Enter a valid phone number, including area code.', 'danger')
    elif send_text and not phone_number:
        flash('A phone number is required to send the welcome text.', 'danger')
    elif EmployeeAccount.query.filter_by(name_key=name_key(employee_name)).first():
        flash('That employee already has an account.', 'danger')
    else:
        employee = EmployeeAccount(
            employee_name=employee_name,
            name_key=name_key(employee_name),
            pin_hash=generate_password_hash(pin),
            active=True,
            must_change_pin=True,
            phone_number=phone_number or None,
        )
        db.session.add(employee)
        db.session.commit()
        flash(f'Account created for {employee_name}.', 'success')
        if send_text:
            try:
                send_login_text(employee_name, phone_number, pin)
                flash(f'Welcome text sent to {phone_number}.', 'success')
            except Exception:
                app.logger.exception('Twilio welcome text failed')
                flash('The account was created, but the welcome text could not be sent. Check Twilio settings and Render logs.', 'warning')
    return redirect(url_for('admin_dashboard'))



@app.route('/admin/employees/<int:employee_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_edit_employee(employee_id):
    employee = db.session.get(EmployeeAccount, employee_id)
    if not employee:
        flash('Employee not found.', 'danger')
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        new_name = clean_name(request.form.get('employee_name', ''))
        phone_input = (request.form.get('phone_number', '') or '').strip()
        phone_number = normalize_phone(phone_input) if phone_input else ''
        active = request.form.get('active') == '1'

        if len(new_name) < 2:
            flash('Enter the employee\'s full name.', 'danger')
            return render_template('edit_employee.html', employee=employee)
        if phone_input and not phone_number:
            flash('Enter a valid phone number, including area code.', 'danger')
            return render_template('edit_employee.html', employee=employee)

        new_key = name_key(new_name)
        duplicate = EmployeeAccount.query.filter(
            EmployeeAccount.name_key == new_key,
            EmployeeAccount.id != employee.id,
        ).first()
        if duplicate:
            flash('Another employee already uses that name.', 'danger')
            return render_template('edit_employee.html', employee=employee)

        old_name = employee.employee_name
        if new_name != old_name:
            # Historical records use employee_name as their link. Avoid merging into
            # historical records left behind by a previously deleted account.
            existing_time = EmployeeTimeEntry.query.filter_by(employee_name=new_name).first()
            existing_email = TimesheetEmailSubmission.query.filter_by(employee_name=new_name).first()
            if existing_time or existing_email:
                flash('That name already has historical timesheet records. Use a different name to avoid combining employee histories.', 'danger')
                return render_template('edit_employee.html', employee=employee)

            EmployeeTimeEntry.query.filter_by(employee_name=old_name).update(
                {'employee_name': new_name}, synchronize_session=False
            )
            TimesheetEmailSubmission.query.filter_by(employee_name=old_name).update(
                {'employee_name': new_name}, synchronize_session=False
            )

        employee.employee_name = new_name
        employee.name_key = new_key
        employee.phone_number = phone_number or None
        employee.active = active
        db.session.commit()
        flash(f'{new_name} was updated successfully.', 'success')
        return redirect(url_for('admin_dashboard'))

    return render_template('edit_employee.html', employee=employee)

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


@app.post('/admin/employees/<int:employee_id>/resend-current-week')
@admin_required
def admin_resend_current_week(employee_id):
    employee = db.session.get(EmployeeAccount, employee_id)
    if not employee:
        flash('Employee not found.', 'danger')
        return redirect(url_for('admin_dashboard'))

    week_start = monday_for(datetime.now(TZ).date())
    try:
        subject, body = build_employee_email(employee.employee_name, week_start)
        send_timesheet_email(subject, body)
        flash(
            f'{employee.employee_name}\'s timesheet for the week of '
            f'{week_start.strftime("%m/%d/%Y")} was resent successfully.',
            'success',
        )
    except Exception:
        app.logger.exception('Admin resend timesheet email failed')
        flash(
            f'{employee.employee_name}\'s timesheet could not be resent. Check the email settings and Render logs.',
            'danger',
        )
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/employees/<int:employee_id>/pin')
@admin_required
def admin_reset_pin(employee_id):
    employee = db.session.get(EmployeeAccount, employee_id)
    pin = (request.form.get('pin', '') or '').strip()
    send_text = request.form.get('send_login_text') == '1'
    if not employee:
        flash('Employee not found.', 'danger')
    elif not valid_pin(pin):
        flash('PIN must be 4–6 digits.', 'danger')
    elif send_text and not employee.phone_number:
        flash('Add a phone number for this employee before sending login info by text.', 'danger')
    else:
        employee.pin_hash = generate_password_hash(pin)
        employee.must_change_pin = True
        db.session.commit()
        flash(f'Temporary PIN reset for {employee.employee_name}. They must choose a new PIN at next login.', 'success')
        if send_text:
            try:
                send_login_text(employee.employee_name, employee.phone_number, pin)
                flash(f'New login text sent to {employee.phone_number}.', 'success')
            except Exception:
                app.logger.exception('Twilio reset PIN text failed')
                flash('The PIN was reset, but the text could not be sent. Check Twilio settings and Render logs.', 'warning')
    return redirect(url_for('admin_dashboard'))


@app.post('/admin/employees/<int:employee_id>/phone')
@admin_required
def admin_update_phone(employee_id):
    employee = db.session.get(EmployeeAccount, employee_id)
    phone_input = (request.form.get('phone_number', '') or '').strip()
    phone_number = normalize_phone(phone_input) if phone_input else ''
    if not employee:
        flash('Employee not found.', 'danger')
    elif phone_input and not phone_number:
        flash('Enter a valid phone number, including area code.', 'danger')
    else:
        employee.phone_number = phone_number or None
        db.session.commit()
        flash(f'Phone number updated for {employee.employee_name}.', 'success')
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


@app.route("/privacy")
def privacy_policy():
    return render_template("privacy.html")

@app.route("/terms")
def terms_of_use():
    return render_template("terms.html")


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')), debug=os.getenv('FLASK_DEBUG') == '1')

