import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'change-me-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///timesheet.db').replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
TZ = ZoneInfo(os.getenv('APP_TIMEZONE', 'America/New_York'))


class TimeEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    work_date = db.Column(db.Date, nullable=False)
    regular_hours = db.Column(db.Float, nullable=False, default=0)
    overtime_hours = db.Column(db.Float, nullable=False, default=0)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint('work_date', name='uq_work_date'),)


def monday_for(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_days(start: date):
    return [start + timedelta(days=i) for i in range(7)]


@app.before_request
def create_tables():
    db.create_all()


@app.route('/')
def index():
    requested = request.args.get('week')
    try:
        base = date.fromisoformat(requested) if requested else datetime.now(TZ).date()
    except ValueError:
        base = datetime.now(TZ).date()

    week_start = monday_for(base)
    days = week_days(week_start)
    entries = TimeEntry.query.filter(TimeEntry.work_date.between(days[0], days[-1])).all()
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
    week_start = date.fromisoformat(request.form['week_start'])
    for d in week_days(week_start):
        key = d.isoformat()
        regular = float(request.form.get(f'regular_{key}', 0) or 0)
        overtime = float(request.form.get(f'overtime_{key}', 0) or 0)
        notes = (request.form.get(f'notes_{key}', '') or '').strip()

        if regular < 0 or overtime < 0 or regular > 24 or overtime > 24 or regular + overtime > 24:
            flash(f'Invalid hours for {d.strftime("%A, %b %d")}. Daily total must be between 0 and 24.', 'danger')
            return redirect(url_for('index', week=week_start.isoformat()))

        entry = TimeEntry.query.filter_by(work_date=d).first()
        if entry:
            entry.regular_hours = regular
            entry.overtime_hours = overtime
            entry.notes = notes
        elif regular or overtime or notes:
            db.session.add(TimeEntry(work_date=d, regular_hours=regular, overtime_hours=overtime, notes=notes))

    db.session.commit()
    flash('Timesheet saved.', 'success')
    return redirect(url_for('index', week=week_start.isoformat()))


@app.get('/health')
def health():
    return {'status': 'ok'}, 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')), debug=os.getenv('FLASK_DEBUG') == '1')
