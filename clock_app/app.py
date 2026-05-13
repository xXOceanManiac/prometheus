from flask import Flask, render_template, request, redirect, url_for
import sqlite3
from datetime import datetime, timedelta
import os

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), 'clockapp.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS time_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL,
                clock_in DATETIME NOT NULL,
                clock_out DATETIME,
                FOREIGN KEY (employee_id) REFERENCES employees(id)
            );
        ''')


@app.route('/')
def index():
    db = get_db()
    employees = db.execute('''
        SELECT e.id, e.name,
               (SELECT tr.clock_in
                FROM time_records tr
                WHERE tr.employee_id = e.id AND tr.clock_out IS NULL
                ORDER BY tr.clock_in DESC LIMIT 1) as clocked_in_at
        FROM employees e
        ORDER BY e.name
    ''').fetchall()
    total_in = sum(1 for e in employees if e['clocked_in_at'])
    db.close()
    return render_template('index.html', employees=employees, total_in=total_in, now=datetime.now())


@app.route('/clock', methods=['POST'])
def clock():
    employee_id = request.form.get('employee_id')
    if not employee_id:
        return redirect(url_for('index'))

    db = get_db()
    record = db.execute(
        'SELECT id FROM time_records WHERE employee_id = ? AND clock_out IS NULL',
        (employee_id,)
    ).fetchone()

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if record:
        db.execute('UPDATE time_records SET clock_out = ? WHERE id = ?', (now, record['id']))
    else:
        db.execute('INSERT INTO time_records (employee_id, clock_in) VALUES (?, ?)', (employee_id, now))

    db.commit()
    db.close()
    return redirect(url_for('index'))


@app.route('/records')
def records():
    employee_id = request.args.get('employee_id', '')
    date_from = request.args.get('date_from', (datetime.now() - timedelta(days=6)).strftime('%Y-%m-%d'))
    date_to = request.args.get('date_to', datetime.now().strftime('%Y-%m-%d'))

    db = get_db()
    employees = db.execute('SELECT id, name FROM employees ORDER BY name').fetchall()

    query = '''
        SELECT tr.id, e.name, tr.clock_in, tr.clock_out,
               CASE WHEN tr.clock_out IS NOT NULL
                    THEN ROUND((julianday(tr.clock_out) - julianday(tr.clock_in)) * 24, 2)
                    ELSE NULL END as hours
        FROM time_records tr
        JOIN employees e ON e.id = tr.employee_id
        WHERE date(tr.clock_in) BETWEEN ? AND ?
    '''
    params = [date_from, date_to]

    if employee_id:
        query += ' AND tr.employee_id = ?'
        params.append(employee_id)

    query += ' ORDER BY tr.clock_in DESC'
    rows = db.execute(query, params).fetchall()
    db.close()

    total_hours = sum(r['hours'] for r in rows if r['hours'] is not None)
    return render_template('records.html',
                           records=rows,
                           employees=employees,
                           selected_employee=employee_id,
                           date_from=date_from,
                           date_to=date_to,
                           total_hours=round(total_hours, 2))


@app.route('/admin', methods=['GET', 'POST'])
def admin():
    db = get_db()
    error = None

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            name = request.form.get('name', '').strip()
            if name:
                existing = db.execute('SELECT id FROM employees WHERE LOWER(name) = LOWER(?)', (name,)).fetchone()
                if existing:
                    error = f'An employee named "{name}" already exists.'
                else:
                    db.execute('INSERT INTO employees (name) VALUES (?)', (name,))
                    db.commit()
                    db.close()
                    return redirect(url_for('admin'))
        elif action == 'delete':
            emp_id = request.form.get('employee_id')
            db.execute('DELETE FROM time_records WHERE employee_id = ?', (emp_id,))
            db.execute('DELETE FROM employees WHERE id = ?', (emp_id,))
            db.commit()
            db.close()
            return redirect(url_for('admin'))

    employees = db.execute('''
        SELECT e.id, e.name, e.created_at,
               COUNT(tr.id) as total_shifts,
               ROUND(SUM(CASE WHEN tr.clock_out IS NOT NULL
                    THEN (julianday(tr.clock_out) - julianday(tr.clock_in)) * 24
                    ELSE 0 END), 1) as total_hours
        FROM employees e
        LEFT JOIN time_records tr ON tr.employee_id = e.id
        GROUP BY e.id
        ORDER BY e.name
    ''').fetchall()
    db.close()
    return render_template('admin.html', employees=employees, error=error)


@app.route('/delete_record', methods=['POST'])
def delete_record():
    record_id = request.form.get('record_id')
    db = get_db()
    db.execute('DELETE FROM time_records WHERE id = ?', (record_id,))
    db.commit()
    db.close()
    return redirect(request.referrer or url_for('records'))


if __name__ == '__main__':
    init_db()
    print('TimeTrack running at http://localhost:5050')
    app.run(debug=True, port=5050)
