import os
import smtplib
import csv
from email.message import EmailMessage
from email.utils import parseaddr
from datetime import date, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, render_template, request, redirect, url_for, flash, session, send_from_directory, send_file
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint, inspect, text
from werkzeug.security import generate_password_hash, check_password_hash
from io import BytesIO, StringIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
import re

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'change-me-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///timesheet.db').replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
DEFAULT_TIMEZONE = os.getenv('APP_TIMEZONE', 'America/New_York')
TZ = ZoneInfo(DEFAULT_TIMEZONE)


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



DEFAULT_SETTINGS = {
    'company_name': 'JC Timesheet',
    'app_name': 'JC Timesheet',
    'overtime_threshold': '40',
    'timezone': DEFAULT_TIMEZONE,
    'weekly_cutoff_time': '17:00',
}

def get_setting(key: str, default=None):
    setting = AppSetting.query.filter_by(key=key).first()
    if setting and setting.value is not None and str(setting.value).strip() != '':
        return setting.value
    if key == 'timesheet_recipients':
        return os.getenv('TIMESHEET_TO_EMAIL', '')
    return DEFAULT_SETTINGS.get(key, default)

def set_setting(key: str, value: str):
    setting = AppSetting.query.filter_by(key=key).first()
    if not setting:
        setting = AppSetting(key=key, value='')
        db.session.add(setting)
    setting.value = str(value)

def app_timezone():
    tz_name = get_setting('timezone', DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)

def overtime_threshold():
    try:
        value = float(get_setting('overtime_threshold', '40'))
        return max(1.0, min(value, 168.0))
    except Exception:
        return 40.0

def selected_week_from_request():
    raw = request.args.get('week')
    try:
        base = date.fromisoformat(raw) if raw else datetime.now(app_timezone()).date()
    except ValueError:
        base = datetime.now(app_timezone()).date()
    return monday_for(base)

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



def valid_email(value: str) -> bool:
    value = (value or '').strip()
    _name, addr = parseaddr(value)
    return bool(addr and addr == value and '@' in addr and '.' in addr.rsplit('@', 1)[-1])


def get_timesheet_recipients():
    raw = get_setting('timesheet_recipients', '') or ''
    normalized = str(raw).replace(';', ',').replace(chr(10), ',')
    return [x.strip() for x in normalized.split(',') if x.strip()]


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
    today = datetime.now(app_timezone()).date()
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
        base = date.fromisoformat(requested) if requested else datetime.now(app_timezone()).date()
    except ValueError:
        base = datetime.now(app_timezone()).date()

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

        regular_available = max(0.0, overtime_threshold() - weekly_regular_used)
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
    week_start = selected_week_from_request()
    week_end = week_start + timedelta(days=6)
    employees = EmployeeAccount.query.order_by(EmployeeAccount.employee_name).all()
    active_employees = [e for e in employees if e.active]

    entries = EmployeeTimeEntry.query.filter(
        EmployeeTimeEntry.work_date.between(week_start, week_end)
    ).all()
    active_names = {e.employee_name for e in active_employees}
    names_with_entries = {
        e.employee_name for e in entries
        if e.employee_name in active_names and ((e.regular_hours or 0) + (e.overtime_hours or 0) > 0 or (e.notes or '').strip())
    }
    total_hours = sum(
        (e.regular_hours or 0) + (e.overtime_hours or 0)
        for e in entries if e.employee_name in active_names
    )
    summary = {
        'active_employees': len(active_employees),
        'timesheets_this_week': len(names_with_entries),
        'missing_timesheets': max(0, len(active_employees) - len(names_with_entries)),
        'hours_this_week': total_hours,
    }
    recipients = get_timesheet_recipients()
    settings = {
        'company_name': get_setting('company_name', 'JC Timesheet'),
        'app_name': get_setting('app_name', 'JC Timesheet'),
        'overtime_threshold': overtime_threshold(),
        'timezone': get_setting('timezone', DEFAULT_TIMEZONE),
        'weekly_cutoff_time': get_setting('weekly_cutoff_time', '17:00'),
    }
    return render_template(
        'admin.html', employees=employees, recipients=recipients, settings=settings,
        summary=summary, week_start=week_start, week_end=week_end
    )



@app.get('/admin/employee/<int:employee_id>/timesheet')
@admin_required
def admin_employee_timesheet(employee_id):
    employee = db.session.get(EmployeeAccount, employee_id)
    if not employee:
        flash('Employee not found.', 'danger')
        return redirect(url_for('admin_dashboard'))

    requested = request.args.get('week')
    try:
        base_day = date.fromisoformat(requested) if requested else datetime.now(app_timezone()).date()
    except ValueError:
        base_day = datetime.now(app_timezone()).date()

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

        regular_available = max(0.0, overtime_threshold() - weekly_regular_used)
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



def pdf_job_info_from_request():
    fields = {
        'customer': (request.args.get('customer') or '').strip(),
        'job_site': (request.args.get('job_site') or '').strip(),
        'job_address': (request.args.get('job_address') or '').strip(),
        'report_to': (request.args.get('report_to') or '').strip(),
        'report_phone': (request.args.get('report_phone') or '').strip(),
        'report_time': (request.args.get('report_time') or '').strip(),
        'duties': (request.args.get('duties') or '').strip(),
        'order_number': (request.args.get('order_number') or '').strip(),
        'customer_id': (request.args.get('customer_id') or '').strip(),
        'time_slip': (request.args.get('time_slip') or '').strip(),
        'customer_po': (request.args.get('customer_po') or '').strip(),
        'ticket_date': (request.args.get('ticket_date') or '').strip(),
        'provider_branch': (request.args.get('provider_branch') or '').strip(),
        'provider_phone': (request.args.get('provider_phone') or '').strip(),
    }
    return {k: v for k, v in fields.items() if v}


@app.get('/admin/employee/<int:employee_id>/timesheet/pdf')
@admin_required
def admin_employee_timesheet_pdf(employee_id):
    employee = db.session.get(EmployeeAccount, employee_id)
    if not employee:
        abort(404)

    week_start = selected_week_from_request()
    week_end = week_start + timedelta(days=6)
    rows, total_regular, total_overtime = get_employee_week_rows(employee.employee_name, week_start)
    total_hours = total_regular + total_overtime
    job_info = pdf_job_info_from_request()

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, rightMargin=32, leftMargin=32,
        topMargin=30, bottomMargin=30,
        title=f'{employee.employee_name} Timesheet'
    )
    styles = getSampleStyleSheet()
    story = []

    company_name = get_setting('company_name', 'JC Timesheet')
    story.append(Paragraph(company_name, styles['Title']))
    story.append(Paragraph(f'{employee.employee_name} - Weekly Timesheet', styles['Heading2']))
    story.append(Paragraph(
        f'Week: {week_start.strftime("%m/%d/%Y")} - {week_end.strftime("%m/%d/%Y")}',
        styles['BodyText']
    ))
    story.append(Spacer(1, 10))

    if job_info:
        labels = [
            ('customer', 'Customer'),
            ('job_site', 'Job Site'),
            ('job_address', 'Job Address'),
            ('report_to', 'Report To'),
            ('report_phone', 'Report Phone'),
            ('report_time', 'Report Time'),
            ('duties', 'Duties'),
            ('order_number', 'Order Number'),
            ('customer_id', 'Customer ID'),
            ('time_slip', 'Time Slip'),
            ('customer_po', 'Customer P.O.'),
            ('ticket_date', 'Ticket Date'),
            ('provider_branch', 'Provider / Branch'),
            ('provider_phone', 'Provider Phone'),
        ]
        info_rows, current = [], []
        for key, label in labels:
            if key in job_info:
                safe_val = job_info[key].replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
                current += [
                    Paragraph(f'<b>{label}</b>', styles['BodyText']),
                    Paragraph(safe_val, styles['BodyText'])
                ]
                if len(current) == 4:
                    info_rows.append(current)
                    current = []
        if current:
            while len(current) < 4:
                current.append('')
            info_rows.append(current)

        info_table = Table(info_rows, colWidths=[78, 185, 78, 185])
        info_table.setStyle(TableStyle([
            ('GRID', (0,0), (-1,-1), 0.4, colors.HexColor('#d7dce2')),
            ('BACKGROUND', (0,0), (0,-1), colors.HexColor('#f3f5f7')),
            ('BACKGROUND', (2,0), (2,-1), colors.HexColor('#f3f5f7')),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('TOPPADDING', (0,0), (-1,-1), 2),
            ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ]))
        story += [info_table, Spacer(1, 14)]

    data = [['Day', 'Date', 'Total', 'Regular', 'OT', 'Notes']]
    for r in rows:
        notes = (r['notes'] or '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
        data.append([
            r['date'].strftime('%A'),
            r['date'].strftime('%m/%d/%Y'),
            f"{r['total']:.2f}",
            f"{r['regular']:.2f}",
            f"{r['overtime']:.2f}",
            Paragraph(notes or '-', styles['BodyText']),
        ])
    data.append(['Totals', '', f'{total_hours:.2f}', f'{total_regular:.2f}', f'{total_overtime:.2f}', ''])

    table = Table(data, colWidths=[66, 66, 48, 54, 42, 246], repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#0d6efd')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('GRID', (0,0), (-1,-1), 0.4, colors.HexColor('#cccccc')),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('ALIGN', (2,1), (4,-1), 'RIGHT'),
        ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor('#e9ecef')),
        ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
    ]))
    story.append(table)
    doc.build(story)
    buffer.seek(0)

    safe_name = re.sub(r'[^A-Za-z0-9_-]+', '_', employee.employee_name).strip('_') or 'employee'
    return send_file(
        buffer, mimetype='application/pdf', as_attachment=True,
        download_name=f'{safe_name}_timesheet_{week_start.isoformat()}.pdf'
    )




def _hq_fmt_date(d):
    return f"{d.month}/{d.day}/{d.year}"

def _hq_draw_rect(pdf, x, y, w, h, line=0.8, fill_color=None):
    pdf.setLineWidth(line)
    if fill_color is not None:
        pdf.setFillColor(fill_color)
        pdf.rect(x, y, w, h, fill=1, stroke=1)
        pdf.setFillColor(colors.black)
    else:
        pdf.rect(x, y, w, h, fill=0, stroke=1)

def _hq_draw_qr_placeholder(pdf, x, y, size):
    pdf.setLineWidth(1)
    pdf.rect(x, y, size, size, fill=0, stroke=1)
    modules = 9
    cell = size / modules
    pattern = {
        (0,0),(1,0),(2,0),(0,1),(2,1),(0,2),(1,2),(2,2),
        (6,0),(7,0),(8,0),(6,1),(8,1),(6,2),(7,2),(8,2),
        (0,6),(1,6),(2,6),(0,7),(2,7),(0,8),(1,8),(2,8),
        (4,1),(5,1),(4,2),(5,3),(4,4),(5,4),(6,4),(5,5),
        (3,6),(4,6),(5,6),(4,7),(6,7),(7,7),(6,8)
    }
    for cx, cy in pattern:
        pdf.rect(x + cx * cell, y + cy * cell, cell, cell, fill=1, stroke=0)

def _hq_draw_label_value(pdf, x, y, label, value, label_size=7, value_size=10):
    pdf.setFont('Helvetica', label_size)
    pdf.drawString(x, y, label)
    pdf.setFont('Helvetica-Bold', value_size)
    pdf.drawString(x, y - 11, value)


def hirequest_employee_display_name(employee_name: str):
    parts = [p for p in (employee_name or '').strip().split() if p]
    if len(parts) >= 2:
        return f'{parts[-1].upper()}, {" ".join(parts[:-1]).upper()}'
    return (employee_name or '').upper()


@app.get('/admin/employee/<int:employee_id>/timesheet/hirequest.pdf')
@admin_required
def admin_employee_hirequest_pdf(employee_id):
    employee = db.session.get(EmployeeAccount, employee_id)
    if not employee:
        abort(404)

    week_start = selected_week_from_request()
    rows, total_regular, total_overtime = get_employee_week_rows(employee.employee_name, week_start)
    daily_hours = [float(r['total'] or 0) for r in rows]
    total_hours = sum(daily_hours)

    page_width, page_height = 768, 522
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(page_width, page_height))
    pdf.setTitle(f'HireQuest Timesheet - {employee.employee_name}')

    def y_from_top(top_value):
        return page_height - top_value

    # Outer border
    pdf.setLineWidth(1)
    pdf.rect(1, 1, page_width - 2, page_height - 2, fill=0, stroke=1)

    # --- Top header blocks ---
    top_h = 88
    left_w, mid_w = 300, 134
    right_w = page_width - left_w - mid_w - 2

    _hq_draw_rect(pdf, 1, page_height - top_h - 1, left_w, top_h, line=0.8)
    _hq_draw_rect(pdf, left_w + 1, page_height - top_h - 1, mid_w, top_h, line=0.8)
    _hq_draw_rect(pdf, left_w + mid_w + 1, page_height - top_h - 1, right_w, top_h, line=0.8)

    # Left address block
    pdf.setFont('Helvetica', 10)
    pdf.drawString(18, y_from_top(18), 'CHAMBLEE, GA')
    pdf.drawString(18, y_from_top(35), '4294 MEMORIAL DR STE C')
    pdf.drawString(18, y_from_top(52), 'DECATUR, GA 30032')
    pdf.setFont('Helvetica', 9)
    pdf.drawString(18, y_from_top(78), 'Phone:')
    pdf.drawString(62, y_from_top(78), '(678) 547-0667')

    # Middle QR / brand block
    _hq_draw_qr_placeholder(pdf, left_w + 28, page_height - 70, 64)
    pdf.setFont('Helvetica-Bold', 11)
    pdf.drawString(left_w + 102, y_from_top(20), 'HireQuest Direct')
    pdf.setFont('Helvetica-Oblique', 8)
    pdf.drawString(left_w + 102, y_from_top(33), 'The Right People at the Right Time')

    # Right order info block
    x0 = left_w + mid_w + 1
    label_fill = colors.HexColor('#222222')
    box_h = 28
    small_w = right_w / 3.0
    titles = [('Order Number', '2189980'), ('Customer ID', '35111'), ('Time Slip', '15426560')]
    for i, (lbl, val) in enumerate(titles):
        x = x0 + i * small_w
        _hq_draw_rect(pdf, x, page_height - box_h - 1, small_w, box_h, line=0.6)
        pdf.setFillColor(label_fill)
        pdf.rect(x, page_height - 12 - 1, small_w, 12, fill=1, stroke=0)
        pdf.setFillColor(colors.white)
        pdf.setFont('Helvetica', 6.5)
        pdf.drawCentredString(x + small_w / 2, page_height - 8, lbl)
        pdf.setFillColor(colors.black)
        pdf.setFont('Helvetica-Bold', 10)
        pdf.drawCentredString(x + small_w / 2, page_height - 24, val)

    # Second header row
    second_y = page_height - top_h - 1
    second_h = 52
    left2_w = 500
    right2_w = page_width - left2_w - 2
    _hq_draw_rect(pdf, 1, second_y - second_h, left2_w, second_h, line=0.8)
    _hq_draw_rect(pdf, left2_w + 1, second_y - second_h, right2_w, second_h, line=0.8)

    # Customer / Job site
    pdf.setFont('Helvetica-Bold', 11)
    pdf.drawString(8, second_y - 18, 'Customer')
    pdf.drawString(8, second_y - 38, 'Job Site')
    pdf.setFont('Helvetica', 10.5)
    pdf.drawString(118, second_y - 18, 'NEW SOUTH CONSTRUCTION')
    pdf.drawString(118, second_y - 34, 'CTCC OASIS')
    pdf.drawString(118, second_y - 50, '155 WEST PACES FERRY RD NW')
    pdf.drawString(118, second_y - 66, 'ATLANTA, GA 30305')

    # Right side: DATE + PO + report info
    rx = left2_w + 10
    pdf.setFont('Helvetica', 9)
    pdf.drawString(rx, second_y - 14, 'DATE')
    pdf.setFont('Helvetica-Bold', 10)
    pdf.drawString(rx + 38, second_y - 14, _hq_fmt_date(week_start))

    po_x, po_w = left2_w + 115, 150
    _hq_draw_rect(pdf, po_x, second_y - 24, po_w, 22, line=0.6)
    pdf.setFillColor(label_fill)
    pdf.rect(po_x, second_y - 2, po_w, 10, fill=1, stroke=0)
    pdf.setFillColor(colors.white)
    pdf.setFont('Helvetica', 6.5)
    pdf.drawCentredString(po_x + po_w / 2, second_y + 4, 'Customer P.O. Number')
    pdf.setFillColor(colors.black)
    pdf.setFont('Helvetica-Bold', 9.5)
    pdf.drawString(po_x + 10, second_y - 15, '25-562')

    pdf.setFont('Helvetica', 8.5)
    pdf.drawString(rx, second_y - 30, 'REPORT TO: JODY 404-952-5115')
    pdf.drawString(rx + 205, second_y - 30, 'TIME: 07:00 AM')
    pdf.drawString(rx, second_y - 46, 'DUTIES: 1 SKILLED - SKILLED')
    pdf.drawString(rx, second_y - 62, 'GATE GUARD')

    # Directions/notes strip
    dir_y = second_y - second_h - 21
    _hq_draw_rect(pdf, 1, dir_y, page_width - 2, 21, line=0.8)
    pdf.setFont('Helvetica', 8.5)
    pdf.drawString(6, dir_y + 6, 'Directions/Notes:')

    # --- Main time table ---
    table_top = dir_y
    table_bottom = 118
    table_h = table_top - table_bottom
    _hq_draw_rect(pdf, 1, table_bottom, page_width - 2, table_h, line=0.8)

    # Column boundaries
    cols = [1, 36, 71, 109, 145, 181, 225, 265, 456, 490, 524, 558, 592, 626, 660, 694, 767]
    for x in cols[1:-1]:
        pdf.line(x, table_bottom, x, table_top)

    header_h = 18
    pdf.setFillColor(colors.HexColor('#1f1f1f'))
    pdf.rect(1, table_top - header_h, page_width - 2, header_h, fill=1, stroke=0)
    pdf.setFillColor(colors.white)
    pdf.setFont('Helvetica', 5.7)
    head_y = table_top - 12
    headers = ['Hat', 'Boots', 'Gloves,\nGlasses', 'Vest', 'Cash', 'Carpool?', 'Class', 'Employee Name (Last, First)', 'MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU', 'TOTAL']
    for i, hdr in enumerate(headers):
        x1, x2 = cols[i], cols[i + 1]
        cx = (x1 + x2) / 2
        if '\n' in hdr:
            a, b = hdr.split('\n')
            pdf.drawCentredString(cx, table_top - 8, a)
            pdf.drawCentredString(cx, table_top - 14, b)
        else:
            pdf.drawCentredString(cx, head_y, hdr)

    # Row lines
    row_h = 24
    first_row_top = table_top - header_h
    row_lines = [first_row_top - i * row_h for i in range(0, 9)]
    for y in row_lines:
        pdf.line(1, y, page_width - 1, y)

    # Filled employee row
    row_y = first_row_top - 16
    pdf.setFillColor(colors.black)
    pdf.setFont('Helvetica', 10)
    pdf.drawString(cols[7] + 4, row_y, 'SKILL')
    pdf.setFont('Helvetica', 15)
    pdf.drawString(cols[8] - 228, row_y, hirequest_employee_display_name(employee.employee_name))

    pdf.setFont('Helvetica-Bold', 10)
    centers = [ (cols[i] + cols[i+1]) / 2 for i in range(8, 15) ]
    for cx, value in zip(centers, daily_hours):
        if abs(value) > 0.001:
            display = str(int(value)) if float(value).is_integer() else f'{value:.2f}'.rstrip('0').rstrip('.')
            pdf.drawCentredString(cx, row_y, display)
    if abs(total_hours) > 0.001:
        total_display = str(int(total_hours)) if float(total_hours).is_integer() else f'{total_hours:.2f}'.rstrip('0').rstrip('.')
        pdf.drawCentredString((cols[15] + cols[16]) / 2, row_y, total_display)

    # Footer note
    note_top = table_bottom
    note_bottom = 78
    _hq_draw_rect(pdf, 1, note_bottom, page_width - 2, note_top - note_bottom, line=0.8)
    pdf.setFont('Helvetica', 7.1)
    pdf.drawString(4, note_top - 14, 'Attention Supervisors: Please fill the hours worked by employees, sign, and tear at perforation.')
    pdf.drawString(4, note_top - 25, 'Return top portion of time ticket and keep the bottom portion for your records. By signing, customer')
    pdf.drawString(4, note_top - 36, 'agrees to the terms on the reverse side of this ticket.')

    # Signature / repeat workers
    sig_bottom = 16
    _hq_draw_rect(pdf, 1, sig_bottom, page_width - 2, note_bottom - sig_bottom, line=0.8)
    repeat_w = 238
    repeat_x = page_width - repeat_w - 1
    _hq_draw_rect(pdf, repeat_x, sig_bottom, repeat_w, note_bottom - sig_bottom, line=0.8)
    pdf.line(repeat_x + 86, sig_bottom, repeat_x + 86, note_bottom)
    pdf.line(repeat_x, sig_bottom + 32, page_width - 1, sig_bottom + 32)

    pdf.setFont('Helvetica-Bold', 8.8)
    pdf.drawString(6, sig_bottom + 7, 'AUTHORIZED SIGNATURE')
    pdf.setFont('Helvetica', 9)
    pdf.drawString(308, sig_bottom + 7, '( Return to HireQuest Direct )')
    pdf.setFont('Helvetica', 5.8)
    pdf.drawCentredString(page_width / 2, 8, 'HireQuest, Inc. publicly traded on NASDAQ as HQI')

    pdf.setFont('Helvetica-Bold', 8.8)
    pdf.drawString(repeat_x + 8, sig_bottom + 38, 'Repeat')
    pdf.drawString(repeat_x + 8, sig_bottom + 26, 'Workers')
    pdf.setFont('Helvetica', 8.8)
    pdf.drawString(repeat_x + 94, sig_bottom + 40, 'Date:')
    pdf.drawString(repeat_x + 94, sig_bottom + 20, 'Time:')
    pdf.drawString(repeat_x + 8, sig_bottom + 8, 'Yes or No')
    pdf.drawString(repeat_x + 94, sig_bottom + 8, '# of Workers:')

    pdf.save()
    buffer.seek(0)

    safe_name = re.sub(r'[^A-Za-z0-9_-]+', '_', employee.employee_name).strip('_') or 'employee'
    return send_file(
        buffer,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f'HireQuest_{safe_name}_{week_start.isoformat()}.pdf'
    )




def newsouth_default_cc(employee_name: str):
    key = (employee_name or '').strip().casefold()
    if 'vicente' in key:
        return '011025.5'
    if 'maria' in key:
        return '011501.5'
    return ''


@app.get('/admin/employee/<int:employee_id>/timesheet/newsouth.pdf')
@admin_required
def admin_employee_newsouth_pdf(employee_id):
    employee = db.session.get(EmployeeAccount, employee_id)
    if not employee:
        abort(404)

    week_start = selected_week_from_request()
    week_end = week_start + timedelta(days=6)
    rows, total_regular, total_overtime = get_employee_week_rows(employee.employee_name, week_start)

    job_number = (request.args.get('job_number') or '25.562').strip()
    cc_number = (request.args.get('cc_number') or newsouth_default_cc(employee.employee_name)).strip()

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=18,
        leftMargin=18,
        topMargin=14,
        bottomMargin=14,
        title=f'NewSouth Timesheet - {employee.employee_name}'
    )
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'NSTitle', parent=styles['Heading1'], fontName='Helvetica-Bold',
        fontSize=11, leading=12, spaceAfter=2
    )
    small = ParagraphStyle(
        'NSSmall', parent=styles['BodyText'], fontSize=7.2, leading=8
    )
    small_center = ParagraphStyle(
        'NSSmallCenter', parent=small, alignment=TA_CENTER
    )

    story = [
        Paragraph('NEW SOUTH CONSTRUCTION COMPANY', title_style),
    ]

    header_data = [
        [
            Paragraph(f'<b>WEEK:</b> {week_start.strftime("%m/%d/%Y")} - {week_end.strftime("%m/%d/%Y")}', small),
            Paragraph('<b>APPROVALS</b>', small_center),
        ],
        [
            Paragraph(f'<b>EMPLOYEE (FULL NAME):</b> {employee.employee_name}', small),
            Paragraph('VP __________________   PM __________________<br/>SUPERINTENDENT __________________', small),
        ],
    ]
    header_table = Table(header_data, colWidths=[360, 180])
    header_table.setStyle(TableStyle([
        ('GRID', (1,0), (1,-1), 0.8, colors.black),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
    ]))
    story += [header_table, Spacer(1, 5)]

    day_headers = []
    for r in rows:
        day_headers.append(
            Paragraph(
                f'<b>{r["date"].strftime("%a").upper()}</b><br/>{r["date"].strftime("%m/%d")}',
                small_center
            )
        )

    table_data = [
        [
            Paragraph('<b>JOB #</b>', small_center),
            Paragraph('<b>CC#</b>', small_center),
            *day_headers,
            Paragraph('<b>TOTALS BY JOB</b>', small_center),
            ''
        ],
        [
            '', '',
            *[Paragraph('<b>Reg</b><br/>OT', small_center) for _ in range(7)],
            Paragraph('<b>REG</b>', small_center),
            Paragraph('<b>OT</b>', small_center),
        ],
        [
            Paragraph(job_number or '-', small_center),
            Paragraph(cc_number or '-', small_center),
            *[f'{r["regular"]:.2f}' if r["regular"] else '' for r in rows],
            f'{total_regular:.2f}',
            f'{total_overtime:.2f}',
        ],
        [
            '',
            Paragraph('<b>OT</b>', small_center),
            *[f'{r["overtime"]:.2f}' if r["overtime"] else '0' for r in rows],
            '',
            f'{total_overtime:.2f}',
        ],
    ]

    # Add six blank rows to match the NewSouth paper layout.
    for _ in range(4):
        table_data.append(['', '', '', '', '', '', '', '', '', '', ''])

    table_data.append([
        Paragraph('<b>DAILY TOTALS</b>', small_center), '',
        *[f'{r["total"]:.2f}' if r["total"] else '0' for r in rows],
        f'{total_regular + total_overtime:.2f}',
        f'{total_overtime:.2f}',
    ])

    col_widths = [62, 62, 55, 55, 55, 55, 55, 55, 55, 62, 48]
    ns_table = Table(table_data, colWidths=col_widths, rowHeights=[20, 20, 22, 20] + [13]*4 + [20])
    yellow = colors.HexColor('#FFF98A')
    ns_table.setStyle(TableStyle([
        ('GRID', (0,0), (-1,-1), 0.7, colors.black),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('FONTNAME', (0,2), (-1,-1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,2), (-1,-1), 7.5),
        ('BACKGROUND', (2,0), (8,0), yellow),
        ('BACKGROUND', (9,0), (10,0), yellow),
        ('BACKGROUND', (10,1), (10,-1), yellow),
        ('BACKGROUND', (2,-1), (10,-1), yellow),
        ('SPAN', (0,0), (0,1)),
        ('SPAN', (1,0), (1,1)),
        ('SPAN', (9,0), (10,0)),
        ('SPAN', (0,-1), (1,-1)),
        ('TOPPADDING', (0,0), (-1,-1), 2),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
    ]))
    story.append(ns_table)

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        f'Regular Hours: {total_regular:.2f} &nbsp;&nbsp;&nbsp; '
        f'Overtime Hours: {total_overtime:.2f} &nbsp;&nbsp;&nbsp; '
        f'Total Hours: {total_regular + total_overtime:.2f}',
        small
    ))

    doc.build(story)
    buffer.seek(0)

    safe_name = re.sub(r'[^A-Za-z0-9_-]+', '_', employee.employee_name).strip('_') or 'employee'
    return send_file(
        buffer,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f'NewSouth_{safe_name}_{week_start.isoformat()}.pdf'
    )



def build_newsouth_employee_table(employee, week_start, job_number='25.562', cc_number=''):
    rows, total_regular, total_overtime = get_employee_week_rows(employee.employee_name, week_start)
    styles = getSampleStyleSheet()
    small = ParagraphStyle('NSSmallMulti', parent=styles['BodyText'], fontSize=7.2, leading=8)
    small_center = ParagraphStyle('NSSmallCenterMulti', parent=small, alignment=TA_CENTER)

    day_headers = [
        Paragraph(
            f'<b>{r["date"].strftime("%a").upper()}</b><br/>{r["date"].strftime("%m/%d")}',
            small_center
        )
        for r in rows
    ]

    table_data = [
        [
            Paragraph('<b>JOB #</b>', small_center),
            Paragraph('<b>CC#</b>', small_center),
            *day_headers,
            Paragraph('<b>TOTALS BY JOB</b>', small_center),
            ''
        ],
        [
            '', '',
            *[Paragraph('<b>Reg</b><br/>OT', small_center) for _ in range(7)],
            Paragraph('<b>REG</b>', small_center),
            Paragraph('<b>OT</b>', small_center),
        ],
        [
            Paragraph(job_number or '-', small_center),
            Paragraph(cc_number or '-', small_center),
            *[f'{r["regular"]:.2f}' if r["regular"] else '' for r in rows],
            f'{total_regular:.2f}',
            f'{total_overtime:.2f}',
        ],
        [
            '',
            Paragraph('<b>OT</b>', small_center),
            *[f'{r["overtime"]:.2f}' if r["overtime"] else '0' for r in rows],
            '',
            f'{total_overtime:.2f}',
        ],
    ]

    for _ in range(4):
        table_data.append(['', '', '', '', '', '', '', '', '', '', ''])

    table_data.append([
        Paragraph('<b>DAILY TOTALS</b>', small_center), '',
        *[f'{r["total"]:.2f}' if r["total"] else '0' for r in rows],
        f'{total_regular + total_overtime:.2f}',
        f'{total_overtime:.2f}',
    ])

    col_widths = [62, 62, 55, 55, 55, 55, 55, 55, 55, 62, 48]
    table = Table(table_data, colWidths=col_widths, rowHeights=[20, 20, 22, 20] + [13]*4 + [20])
    yellow = colors.HexColor('#FFF98A')
    table.setStyle(TableStyle([
        ('GRID', (0,0), (-1,-1), 0.7, colors.black),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('FONTNAME', (0,2), (-1,-1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,2), (-1,-1), 7.5),
        ('BACKGROUND', (2,0), (8,0), yellow),
        ('BACKGROUND', (9,0), (10,0), yellow),
        ('BACKGROUND', (10,1), (10,-1), yellow),
        ('BACKGROUND', (2,-1), (10,-1), yellow),
        ('SPAN', (0,0), (0,1)),
        ('SPAN', (1,0), (1,1)),
        ('SPAN', (9,0), (10,0)),
        ('SPAN', (0,-1), (1,-1)),
        ('TOPPADDING', (0,0), (-1,-1), 2),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
    ]))
    return table, rows, total_regular, total_overtime


@app.get('/admin/reports/newsouth.pdf')
@admin_required
def admin_newsouth_multi_pdf():
    week_start = selected_week_from_request()
    week_end = week_start + timedelta(days=6)

    selected_ids = request.args.getlist('employee_id', type=int)
    if not selected_ids:
        flash('Select at least one employee for the NewSouth PDF.', 'warning')
        return redirect(url_for('admin_dashboard', week=week_start.isoformat()))

    employees = (
        EmployeeAccount.query
        .filter(EmployeeAccount.id.in_(selected_ids))
        .order_by(EmployeeAccount.employee_name)
        .all()
    )
    if not employees:
        flash('No selected employees were found.', 'warning')
        return redirect(url_for('admin_dashboard', week=week_start.isoformat()))

    job_number = (request.args.get('job_number') or '25.562').strip()

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=18,
        leftMargin=18,
        topMargin=14,
        bottomMargin=14,
        title=f'NewSouth Timesheets {week_start.isoformat()}'
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'NSMultiTitle', parent=styles['Heading1'], fontName='Helvetica-Bold',
        fontSize=11, leading=12, spaceAfter=2
    )
    small = ParagraphStyle(
        'NSMultiSmall', parent=styles['BodyText'], fontSize=7.2, leading=8
    )
    small_center = ParagraphStyle(
        'NSMultiSmallCenter', parent=small, alignment=TA_CENTER
    )

    story = []
    for idx, employee in enumerate(employees):
        cc_number = newsouth_default_cc(employee.employee_name)

        story.append(Paragraph('NEW SOUTH CONSTRUCTION COMPANY', title_style))
        header_data = [
            [
                Paragraph(
                    f'<b>WEEK:</b> {week_start.strftime("%m/%d/%Y")} - {week_end.strftime("%m/%d/%Y")}',
                    small
                ),
                Paragraph('<b>APPROVALS</b>', small_center),
            ],
            [
                Paragraph(f'<b>EMPLOYEE (FULL NAME):</b> {employee.employee_name}', small),
                Paragraph('VP __________________   PM __________________<br/>SUPERINTENDENT __________________', small),
            ],
        ]
        header_table = Table(header_data, colWidths=[360, 180])
        header_table.setStyle(TableStyle([
            ('GRID', (1,0), (1,-1), 0.8, colors.black),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('TOPPADDING', (0,0), (-1,-1), 2),
            ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ]))
        story += [header_table, Spacer(1, 5)]

        ns_table, rows, total_regular, total_overtime = build_newsouth_employee_table(
            employee, week_start, job_number, cc_number
        )
        story.append(ns_table)
        story.append(Spacer(1, 3))
        story.append(Paragraph(
            f'Regular Hours: {total_regular:.2f} &nbsp;&nbsp;&nbsp; '
            f'Overtime Hours: {total_overtime:.2f} &nbsp;&nbsp;&nbsp; '
            f'Total Hours: {total_regular + total_overtime:.2f}',
            small
        ))

        if idx < len(employees) - 1:
            story.append(Spacer(1, 18))

    doc.build(story)
    buffer.seek(0)

    return send_file(
        buffer,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f'NewSouth_Selected_{week_start.isoformat()}.pdf'
    )


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



@app.post('/admin/settings')
@admin_required
def admin_settings():
    company_name = clean_name(request.form.get('company_name', '')) or 'JC Timesheet'
    app_name = clean_name(request.form.get('app_name', '')) or 'JC Timesheet'

    try:
        threshold = float(request.form.get('overtime_threshold', '40'))
        if threshold <= 0 or threshold > 168:
            raise ValueError
    except ValueError:
        flash('Overtime threshold must be between 1 and 168 hours.', 'danger')
        return redirect(url_for('admin_dashboard'))

    timezone_name = (request.form.get('timezone', '') or '').strip()
    try:
        ZoneInfo(timezone_name)
    except Exception:
        flash('Enter a valid timezone such as America/New_York.', 'danger')
        return redirect(url_for('admin_dashboard'))

    cutoff = (request.form.get('weekly_cutoff_time', '') or '').strip()
    if not re.match(r'^(?:[01]\d|2[0-3]):[0-5]\d$', cutoff):
        flash('Weekly cutoff time must be a valid time.', 'danger')
        return redirect(url_for('admin_dashboard'))

    raw = request.form.get('recipients', '') or ''
    parts = [x.strip() for x in raw.replace(';', ',').replace(chr(10), ',').split(',') if x.strip()]
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
        flash('Invalid email addresses: ' + ', '.join(invalid), 'danger')
        return redirect(url_for('admin_dashboard'))
    if not recipients:
        flash('Enter at least one valid email recipient.', 'danger')
        return redirect(url_for('admin_dashboard'))

    set_setting('company_name', company_name)
    set_setting('app_name', app_name)
    set_setting('overtime_threshold', threshold)
    set_setting('timezone', timezone_name)
    set_setting('weekly_cutoff_time', cutoff)
    set_setting('timesheet_recipients', ','.join(recipients))
    db.session.commit()
    flash('Admin settings updated.', 'success')
    return redirect(url_for('admin_dashboard'))


def selected_report_employees():
    selected_ids = request.args.getlist('employee_id', type=int)
    query = EmployeeAccount.query.order_by(EmployeeAccount.employee_name)
    if selected_ids:
        return query.filter(EmployeeAccount.id.in_(selected_ids)).all()
    return []


def build_bulk_timesheet_pdf(week_start: date, employees):
    week_end = week_start + timedelta(days=6)
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30,
        title=f'Weekly Timesheet Report {week_start.isoformat()}'
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph(get_setting('company_name', 'JC Timesheet'), styles['Title']),
        Paragraph(
            f'Weekly Timesheet Report: {week_start.strftime("%m/%d/%Y")} - {week_end.strftime("%m/%d/%Y")}',
            styles['Heading2']
        ),
        Spacer(1, 12),
    ]
    has_data = False
    for employee in employees:
        rows, total_regular, total_overtime = get_employee_week_rows(employee.employee_name, week_start)
        if not any(r['total'] or (r['notes'] or '').strip() for r in rows):
            continue
        has_data = True
        story.append(Paragraph(employee.employee_name, styles['Heading2']))
        data = [['Day', 'Date', 'Total', 'Regular', 'OT', 'Notes']]
        for row in rows:
            notes = (row['notes'] or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            data.append([
                row['date'].strftime('%a'),
                row['date'].strftime('%m/%d/%Y'),
                f"{row['total']:.2f}",
                f"{row['regular']:.2f}",
                f"{row['overtime']:.2f}",
                Paragraph(notes or '-', styles['BodyText']),
            ])
        data.append(['Totals', '', f'{total_regular + total_overtime:.2f}', f'{total_regular:.2f}', f'{total_overtime:.2f}', ''])
        table = Table(data, colWidths=[46, 66, 42, 48, 38, 280], repeatRows=1)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#0d6efd')),
            ('TEXTCOLOR', (0,0), (-1,0), colors.white),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('GRID', (0,0), (-1,-1), 0.4, colors.HexColor('#cccccc')),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('ALIGN', (2,1), (4,-1), 'RIGHT'),
            ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor('#e9ecef')),
            ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
            ('TOPPADDING', (0,0), (-1,-1), 2),
            ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ]))
        story.extend([table, Spacer(1, 18)])
    if not has_data:
        story.append(Paragraph('No timesheet entries were found for this week.', styles['BodyText']))
    doc.build(story)
    buffer.seek(0)
    return buffer


@app.get('/admin/reports/weekly.pdf')
@admin_required
def admin_bulk_weekly_pdf():
    week_start = selected_week_from_request()
    employees = selected_report_employees()
    if not employees:
        flash('Select at least one employee for the bulk report.', 'warning')
        return redirect(url_for('admin_dashboard', week=week_start.isoformat()))
    pdf = build_bulk_timesheet_pdf(week_start, employees)
    return send_file(
        pdf, mimetype='application/pdf', as_attachment=True,
        download_name=f'weekly_timesheets_{week_start.isoformat()}.pdf'
    )


@app.get('/admin/reports/weekly.csv')
@admin_required
def admin_bulk_weekly_csv():
    week_start = selected_week_from_request()
    employees = selected_report_employees()
    if not employees:
        flash('Select at least one employee for the bulk report.', 'warning')
        return redirect(url_for('admin_dashboard', week=week_start.isoformat()))
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Employee', 'Day', 'Date', 'Total Hours', 'Regular Hours', 'Overtime Hours', 'Notes'])
    for employee in employees:
        rows, total_regular, total_overtime = get_employee_week_rows(employee.employee_name, week_start)
        wrote = False
        for row in rows:
            if row['total'] or (row['notes'] or '').strip():
                wrote = True
                writer.writerow([
                    employee.employee_name, row['date'].strftime('%A'), row['date'].isoformat(),
                    f"{row['total']:.2f}", f"{row['regular']:.2f}", f"{row['overtime']:.2f}",
                    row['notes'] or ''
                ])
        if wrote:
            writer.writerow([
                employee.employee_name, 'WEEK TOTAL', '',
                f'{total_regular + total_overtime:.2f}', f'{total_regular:.2f}', f'{total_overtime:.2f}', ''
            ])
    data = output.getvalue().encode('utf-8-sig')
    return send_file(
        BytesIO(data), mimetype='text/csv; charset=utf-8', as_attachment=True,
        download_name=f'weekly_timesheets_{week_start.isoformat()}.csv'
    )


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
        employee = EmployeeAccount(
            employee_name=employee_name,
            name_key=name_key(employee_name),
            pin_hash=generate_password_hash(pin),
            active=True,
            must_change_pin=True,
        )
        db.session.add(employee)
        db.session.commit()
        flash(f'Account created for {employee_name}.', 'success')
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
        active = request.form.get('active') == '1'

        if len(new_name) < 2:
            flash('Enter the employee\'s full name.', 'danger')
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

    week_start = monday_for(datetime.now(app_timezone()).date())
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
    if not employee:
        flash('Employee not found.', 'danger')
    elif not valid_pin(pin):
        flash('PIN must be 4–6 digits.', 'danger')
    else:
        employee.pin_hash = generate_password_hash(pin)
        employee.must_change_pin = True
        db.session.commit()
        flash(f'Temporary PIN reset for {employee.employee_name}. They must choose a new PIN at next login.', 'success')
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

