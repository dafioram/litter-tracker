import sqlite3
import pandas as pd
import os
import shutil
import io
import csv
import json
import re
import hmac
import secrets
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect, url_for, flash, session, abort, Response

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # Uploads are small CSVs

# OPTIONAL LOGIN: Set APP_PASSWORD in .env to require a username/password
APP_USERNAME = os.environ.get('APP_USERNAME', 'admin')
APP_PASSWORD = os.environ.get('APP_PASSWORD', '')

# TIMEZONE: How to convert CSV timestamps to local wall-clock time.
# TIMEZONE (e.g. America/New_York) treats CSV times as UTC and converts them,
# following daylight saving. Otherwise TIMEZONE_OFFSET hours are subtracted
# (Default to 5 hours for EST); use 0 if your CSV times are already local.
LOCAL_TZ = ZoneInfo(os.environ['TIMEZONE']) if os.environ.get('TIMEZONE') else None
TIMEZONE_OFFSET = int(os.environ.get('TIMEZONE_OFFSET', 5))

# --- CONFIGURATION ---
DB_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
if not os.path.exists(DB_FOLDER):
    os.makedirs(DB_FOLDER)
DB_NAME = os.path.join(DB_FOLDER, 'litter_history.db')
BACKUP_FOLDER = os.path.join(DB_FOLDER, 'backups')

# SECURITY: Load secret key from .env. If it's missing (or still the example
# placeholder), generate one and keep it in data/ so sessions survive restarts.
def load_secret_key():
    key = os.environ.get('SECRET_KEY', '')
    if key and key != 'change_this_to_a_random_string':
        return key
    path = os.path.join(DB_FOLDER, '.secret_key')
    if not os.path.exists(path):
        # Write to a temp file and link it into place so concurrent gunicorn
        # workers all end up reading the same complete key
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, 'w') as f:
            f.write(secrets.token_hex(32))
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
        except OSError:
            if not os.path.exists(path): os.replace(tmp, path)
        finally:
            if os.path.exists(tmp): os.remove(tmp)
    with open(path) as f:
        return f.read().strip()

app.secret_key = load_secret_key()

# Tolerance for classification (lbs)
WEIGHT_TOLERANCE = 2.0 

# DWELL TIME: The robot starts cleaning this many minutes after the cat leaves
# (the LR4 "Clean Cycle Wait Time" setting: 3, 7 or 15).
CLEAN_CYCLE_WAIT_MINUTES = int(os.environ.get('CLEAN_CYCLE_WAIT_MINUTES', 7))
# Calculated dwell times above this are usually a missed event in the CSV
# (e.g. the cat left and came back), so they're sent for manual entry instead.
DWELL_MAX_MINUTES = float(os.environ.get('DWELL_MAX_MINUTES', 6))

# --- DATABASE SETUP ---
def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL;') 
    conn.execute('PRAGMA busy_timeout=5000;') 
    conn.execute('PRAGMA synchronous=NORMAL;')
    return conn

def init_db():
    if not os.path.exists(BACKUP_FOLDER):
        os.makedirs(BACKUP_FOLDER)
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS usage_logs (timestamp TEXT PRIMARY KEY, date TEXT, time TEXT, weight REAL, activity TEXT, metadata TEXT, cat_identity TEXT, flag_reason TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS upload_history (id INTEGER PRIMARY KEY AUTOINCREMENT, upload_date TEXT, filename TEXT, entries_added INTEGER)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS data_blacklist (timestamp TEXT, weight REAL, reason TEXT)''')
    
    # NEW: Cat Profiles Table with BIRTHDAY
    conn.execute('''CREATE TABLE IF NOT EXISTS cat_profiles (
        name TEXT PRIMARY KEY, 
        target_weight REAL, 
        color_hex TEXT,
        birthday TEXT
    )''')
    
    # Manually entered dwell times, keyed by the cycle's start timestamp.
    # minutes = NULL means "ignore this cycle".
    conn.execute('''CREATE TABLE IF NOT EXISTS dwell_manual (cycle_timestamp TEXT PRIMARY KEY, minutes REAL)''')

    # MIGRATION CHECK: If you already created the table without birthday, add it now
    try:
        conn.execute("ALTER TABLE cat_profiles ADD COLUMN birthday TEXT")
    except sqlite3.OperationalError:
        pass # Column likely already exists
    
    conn.commit()
    conn.close()

# --- REQUEST SECURITY ---
@app.before_request
def require_login():
    if not APP_PASSWORD: return
    auth = request.authorization
    if not (auth and hmac.compare_digest((auth.username or '').encode(), APP_USERNAME.encode())
            and hmac.compare_digest((auth.password or '').encode(), APP_PASSWORD.encode())):
        return Response('Login required', 401, {'WWW-Authenticate': 'Basic realm="Litter Tracker"'})

def csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(16)
    return session['csrf_token']

app.jinja_env.globals['csrf_token'] = csrf_token

@app.before_request
def csrf_protect():
    # Every form includes {{ csrf_token() }} so other sites can't submit changes
    if request.method == 'POST':
        token = session.get('csrf_token', '')
        if not token or not hmac.compare_digest(token.encode(), request.form.get('csrf_token', '').encode()):
            abort(400, 'Invalid or missing form token. Reload the page and try again.')

def js_json(obj):
    """JSON that is safe to embed inside a <script> tag (NaN becomes null)."""
    text = json.dumps(obj).replace('NaN', 'null')
    return text.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')

# --- CLASSIFICATION LOGIC (UPDATED) ---
def classify_row(row, profiles):
    """
    row: dict/row containing 'activity' and 'weight'
    profiles: list of dicts [{'name': 'Luna', 'target_weight': 10.5}, ...]
    """
    activity = str(row.get('activity', '')).lower()
    weight = row.get('weight', 0.0)
    
    # 1. System Checks
    sys_keywords = ['clean', 'cycle', 'reset', 'power', 'bonnet', 'ready', 'full']
    if any(k in activity for k in sys_keywords): return "System", "Machine Operation"
    
    # 2. Motion / Low Weight
    if 'cat detected' in activity and weight < 0.5:
        return "Unknown", "Motion detected (No weight)"
    if pd.isna(weight) or weight < 0.5: 
        return "Error", f"Weight too low ({weight} lbs)"

    # 3. Nearest Neighbor Match
    best_match = "Unknown"
    closest_diff = 99.9
    reason = "No matching profile"

    for cat in profiles:
        diff = abs(weight - cat['target_weight'])
        if diff < closest_diff:
            closest_diff = diff
            best_match = cat['name']
    
    # 4. Validation
    if closest_diff <= WEIGHT_TOLERANCE:
        return best_match, ""
    else:
        return "Unknown", f"No match within {WEIGHT_TOLERANCE}lbs (Closest: {best_match} @ {closest_diff:.1f} diff)"

# --- TIMESTAMP PARSING ---
def export_date_from_filename(filename):
    """
    Whisker names exports like 'litter-robot_4_activity_2026-10-04.csv'.
    Returns that date as a datetime, or None if the name has no date.
    """
    match = re.search(r'(\d{4})-(\d{2})-(\d{2})', filename or '')
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass
    return None

def parse_whisker_timestamp(raw_ts, latest_allowed):
    """
    Whisker timestamps have no year ('10/4 7:01 am'). Picks the most recent year
    that doesn't put the entry after latest_allowed, so a January export that
    still contains December entries keeps those in the previous year.
    Raises ValueError if the timestamp can't be parsed.
    """
    parts = raw_ts.split()
    month, day = map(int, parts[0].split('/'))
    hour, minute = map(int, parts[1].split(':'))
    if parts[2].lower() == 'pm' and hour != 12: hour += 12
    elif parts[2].lower() == 'am' and hour == 12: hour = 0

    for year in (latest_allowed.year, latest_allowed.year - 1):
        try:
            dt = datetime(year, month, day, hour, minute)
        except ValueError:
            continue  # e.g. Feb 29 in a non-leap year
        if dt <= latest_allowed:
            return dt
    raise ValueError(f"No valid year for timestamp '{raw_ts}'")

def csv_time_to_local(dt):
    """Converts a naive CSV timestamp to naive local time per the settings above."""
    if LOCAL_TZ:
        return dt.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ).replace(tzinfo=None)
    return dt - timedelta(hours=TIMEZONE_OFFSET)

# --- DATE RANGES ---
RANGE_PRESETS = {'30': 30, '90': 90, '180': 180, '365': 365}

def date_range_from_args(args, default='90'):
    """
    Reads ?range=30|90|180|365|all|custom (&start=&end= for custom, YYYY-MM-DD).
    Returns a dict with the chosen preset, display dates, and SQL bounds
    (sql_start inclusive, sql_end exclusive) for filtering on timestamp.
    """
    today = datetime.now().date()
    preset = args.get('range', default)
    start = end = None
    if preset == 'custom':
        try:
            start = datetime.strptime(args.get('start', ''), '%Y-%m-%d').date()
            end = datetime.strptime(args.get('end', ''), '%Y-%m-%d').date()
        except ValueError:
            start = end = None
        if not start or start > end: preset = default
    if preset == 'all':
        start, end = None, today
    elif preset != 'custom':
        if preset not in RANGE_PRESETS: preset = default
        start, end = today - timedelta(days=RANGE_PRESETS[preset]), today
    return {
        'preset': preset,
        'start': start.isoformat() if start else '',
        'end': end.isoformat(),
        'sql_start': start.isoformat() if start else '0000-00-00',
        'sql_end': (end + timedelta(days=1)).isoformat(),
    }

# --- DWELL TIME ---
def compute_dwell_times(df, manual):
    """
    Estimates how long the cat spent in the globe for each clean cycle.
    The robot starts cleaning CLEAN_CYCLE_WAIT_MINUTES after the cat leaves, so
    exit = cycle start - wait, and dwell = exit - the last 'Cat detected' before it.

    df: usage_logs rows with a 'dt' column, sorted by time
    manual: {cycle_timestamp: minutes, or None to ignore}
    Returns one dict per cycle. status is 'calculated', 'manual', 'ignored' or
    'needs_input' (with a reason) when the CSV doesn't support a trustworthy value.
    """
    df = df.assign(minute=df['dt'].dt.floor('min'))
    starts = df[df['activity'] == 'Clean Cycle In Progress']
    detections = df[df['activity'].str.contains('Cat detected', case=False)]
    weights = df[(df['activity'] == 'Weight recorded') & (df['weight'] > 0.5)]
    real_cat = lambda name: name not in ('Unknown', 'System', 'Error')

    results = []
    prev_start = None
    for _, start in starts.iterrows():
        exit_time = start['minute'] - timedelta(minutes=CLEAN_CYCLE_WAIT_MINUTES)
        # Only look back to the previous cycle so one visit isn't counted twice
        lookback = exit_time - timedelta(minutes=60)
        if prev_start is not None: lookback = max(lookback, prev_start)
        prev_start = start['minute']

        in_window = lambda rows: rows[(rows['minute'] > lookback) & (rows['minute'] <= exit_time)]
        entry_rows, weight_rows = in_window(detections), in_window(weights)

        cat = 'Unknown'
        if not entry_rows.empty and real_cat(entry_rows.iloc[-1]['cat_identity']):
            cat = entry_rows.iloc[-1]['cat_identity']
        elif not weight_rows.empty and real_cat(weight_rows.iloc[-1]['cat_identity']):
            cat = weight_rows.iloc[-1]['cat_identity']

        entry = {'cycle_timestamp': start['timestamp'], 'cat': cat, 'calculated': None, 'minutes': None, 'reason': ''}
        if not entry_rows.empty:
            entry['calculated'] = round((exit_time - entry_rows.iloc[-1]['minute']).total_seconds() / 60, 1)

        if start['timestamp'] in manual:
            entry['minutes'] = manual[start['timestamp']]
            entry['status'] = 'ignored' if entry['minutes'] is None else 'manual'
        elif entry['calculated'] is None:
            entry['status'] = 'needs_input'
            entry['reason'] = "No 'Cat detected' logged before this cycle"
        elif not 0 <= entry['calculated'] <= DWELL_MAX_MINUTES:
            entry['status'] = 'needs_input'
            entry['reason'] = f"Calculated {entry['calculated']:g} min is outside the expected 0-{DWELL_MAX_MINUTES:g} min"
        else:
            entry['status'] = 'calculated'
            entry['minutes'] = entry['calculated']
        results.append(entry)
    return results

def load_manual_dwell(conn):
    return {r['cycle_timestamp']: r['minutes'] for r in conn.execute("SELECT cycle_timestamp, minutes FROM dwell_manual")}

# --- ROUTES ---

@app.route('/')
def dashboard():
    init_db()
    conn = get_db()
    profiles = conn.execute("SELECT * FROM cat_profiles").fetchall()
    
    # Rolling year (not Jan 1) so stats don't reset to empty every January
    one_year_ago = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
    df = pd.read_sql_query("SELECT * FROM usage_logs WHERE timestamp >= ? ORDER BY timestamp ASC", conn, params=(one_year_ago,))
    
    thirty_days_ago_dt = datetime.now() - timedelta(days=30)
    cycle_count = 0; interrupt_count = 0; review_count = 0
    
    if not df.empty:
        thirty_days_ago = thirty_days_ago_dt.strftime('%Y-%m-%d %H:%M:%S')
        # Count completions only: each cycle also logs a "Clean Cycle In Progress" row
        cycle_count = conn.execute("SELECT COUNT(*) FROM usage_logs WHERE activity = 'Clean Cycle Complete' AND timestamp > ?", (thirty_days_ago,)).fetchone()[0]
        interrupt_count = conn.execute("SELECT COUNT(*) FROM usage_logs WHERE activity LIKE '%interrupted%' AND timestamp > ?", (thirty_days_ago,)).fetchone()[0]
        review_count = conn.execute("SELECT COUNT(*) FROM usage_logs WHERE (flag_reason != '' OR cat_identity = 'Error' OR cat_identity = 'Unknown') AND cat_identity != 'System'").fetchone()[0]
    
    conn.close()

    trends = {}
    last_entry = None
    data_age_days = 0
    age_status = "good"
    bags_used = round(cycle_count / 17, 1)

    if not df.empty:
        df['dt'] = pd.to_datetime(df['timestamp'], format='%Y-%m-%d %H:%M:%S')
        last_ts = df.iloc[-1]['dt']
        last_entry = df.iloc[-1].to_dict()
        data_age_days = (datetime.now() - last_ts).days
        if data_age_days > 25: age_status = "danger"
        elif data_age_days > 15: age_status = "warning"

    for cat_row in profiles:
        cat_name = cat_row['name']
        curr_w = 0.0
        true_visits = 0
        avg_daily = 0.0
        
        # --- NEW: AGE CALCULATION ---
        age_str = "Age: N/A"
        if cat_row['birthday']:
            try:
                bday = datetime.strptime(cat_row['birthday'], '%Y-%m-%d')
                today = datetime.now()
                # Calculate age in years and months
                years = today.year - bday.year - ((today.month, today.day) < (bday.month, bday.day))
                months = (today.year - bday.year) * 12 + today.month - bday.month
                if years > 0:
                    age_str = f"{years} yr {months % 12} mo"
                else:
                    age_str = f"{months} months"
            except: pass

        if not df.empty:
            cat_df = df[df['cat_identity'] == cat_name]
            if not cat_df.empty:
                # STAT 1: Current Weight Only (No Change Stat)
                valid_weights = cat_df[cat_df['weight'] > 0.5]
                if not valid_weights.empty:
                    curr_w = valid_weights.iloc[-1]['weight']

                # STAT 2: True Visits
                recent_df = cat_df[cat_df['dt'] > thirty_days_ago_dt].sort_values('dt')
                last_time = None
                for _, row in recent_df.iterrows():
                    if last_time is None: true_visits += 1; last_time = row['dt']
                    else:
                        if (row['dt'] - last_time).total_seconds() / 60 > 10:
                            true_visits += 1; last_time = row['dt']
                
                if not recent_df.empty:
                    first_visit = recent_df.iloc[0]['dt']
                    days_tracked = (datetime.now() - first_visit).days
                    divisor = max(1, min(days_tracked + 1, 30))
                    avg_daily = round(true_visits / divisor, 1)
        
        trends[cat_name] = {
            "current": round(curr_w, 2),
            "age_str": age_str,  # <--- Sending Age string instead of weight change
            "visits_total": true_visits,
            "avg_daily": avg_daily,
            "color": cat_row['color_hex'],
            "target": cat_row['target_weight']
        }

    return render_template('dashboard.html', 
        trends=trends, 
        profiles=profiles, 
        last_entry=last_entry, 
        review_count=review_count, 
        cycle_count=cycle_count, 
        interrupt_count=interrupt_count, 
        bags_used=bags_used, 
        data_age=data_age_days, 
        age_status=age_status)

@app.route('/manage_cats', methods=['POST'])
def manage_cats():
    action = request.form.get('action')
    conn = get_db()
    
    if action == 'add':
        name = (request.form.get('name') or '').strip()
        color = request.form.get('color', '')
        birthday = request.form.get('birthday') # <--- Get Birthday
        try:
            weight = float(request.form.get('weight'))
        except (TypeError, ValueError):
            weight = None

        if not name or weight is None:
            flash("Name and target weight are required.", "error")
        elif not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
            flash("Invalid color.", "error")
        else:
            try:
                # Insert including birthday
                conn.execute("INSERT INTO cat_profiles (name, target_weight, color_hex, birthday) VALUES (?, ?, ?, ?)",
                             (name, weight, color, birthday))
                flash(f"Added {name}!", "success")
            except sqlite3.IntegrityError:
                flash("Cat name already exists.", "error")
            
    elif action == 'delete':
        name = request.form.get('name')
        conn.execute("DELETE FROM cat_profiles WHERE name = ?", (name,))
        flash(f"Deleted profile for {name}. History remains.", "warning")
        
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/review')
def review():
    conn = get_db()
    logs = conn.execute("SELECT * FROM usage_logs WHERE (cat_identity IN ('Error', 'Unknown') OR flag_reason != '') AND cat_identity != 'System' ORDER BY timestamp DESC").fetchall()
    # Fetch profiles to generate buttons dynamically
    profiles = conn.execute("SELECT * FROM cat_profiles").fetchall()
    conn.close()
    return render_template('review.html', logs=logs, profiles=profiles)

@app.route('/fix', methods=['POST'])
def fix_entry():
    conn = get_db()
    
    # CLEAN THE ID: Remove leading/trailing spaces or newlines that breaks the DB lookup
    timestamp_id = request.form.get('timestamp', '').strip()
    action = request.form.get('action')
    cat = request.form.get('cat')
    
    if action == 'delete':
        conn.execute("DELETE FROM usage_logs WHERE timestamp = ?", (timestamp_id,))
        flash(f"Deleted record.", "success")

    elif action == 'blacklist':
        row = conn.execute("SELECT timestamp, weight, activity FROM usage_logs WHERE timestamp = ?", (timestamp_id,)).fetchone()
        if row:
            conn.execute("INSERT INTO data_blacklist (timestamp, weight, reason) VALUES (?, ?, ?)", 
                         (row['timestamp'], row['weight'], row['activity']))
            conn.execute("DELETE FROM usage_logs WHERE timestamp = ?", (timestamp_id,))
            flash(f"Blacklisted record.", "warning")
        else:
            flash("Could not find record to blacklist.", "error")

    elif action == 'restore':
        # 1. Try exact match first
        row = conn.execute("SELECT * FROM data_blacklist WHERE timestamp = ?", (timestamp_id,)).fetchone()
        
        if row:
            try:
                # Re-construct the date objects
                dt_obj = datetime.strptime(row['timestamp'], '%Y-%m-%d %H:%M:%S')
                
                # Insert back into active logs
                # We use row['reason'] (which stores the activity name) and default to 'Unknown' cat
                conn.execute('INSERT INTO usage_logs (timestamp, date, time, weight, activity, metadata, cat_identity, flag_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', 
                             (row['timestamp'], dt_obj.strftime('%Y-%m-%d'), dt_obj.strftime('%H:%M:%S'), 
                              row['weight'], row['reason'], '{}', 'Unknown', 'Restored from Blacklist'))
                
                # Remove from blacklist
                conn.execute("DELETE FROM data_blacklist WHERE timestamp = ?", (timestamp_id,))
                flash("Restored record.", "success")
            except Exception as e:
                flash(f"Error restoring: {e}", "error")
        else:
            # Debugging Help: If it fails, tell us why
            flash(f"Restore Failed: Could not find blacklist ID '{timestamp_id}'", "error")

    # Cat Assignment (sent as a separate field, so any cat name works)
    elif cat:
        if conn.execute("SELECT 1 FROM cat_profiles WHERE name = ?", (cat,)).fetchone():
            conn.execute("UPDATE usage_logs SET cat_identity = ?, flag_reason = '' WHERE timestamp = ?", (cat, timestamp_id))
            flash(f"Re-assigned to {cat}", "success")
        else:
            flash(f"No cat profile named '{cat}'.", "error")

    else:
        flash("Unknown action.", "error")
        
    conn.commit()
    conn.close()
    
    # Redirect back to where we came from (The Editor Page)
    return redirect(request.referrer or url_for('dashboard'))

@app.route('/analysis')
def analysis():
    conn = get_db()
    profiles = conn.execute("SELECT * FROM cat_profiles").fetchall()
    
    # Create Dynamic Color Map
    colors = {row['name']: row['color_hex'] for row in profiles}
    colors['Unknown'] = "#999999"
    colors['System'] = "#ffcd56"
    
    date_range = date_range_from_args(request.args, default='365')
    df = pd.read_sql_query("SELECT * FROM usage_logs WHERE cat_identity != 'Error' AND timestamp >= ? AND timestamp < ? ORDER BY timestamp ASC",
                           conn, params=(date_range['sql_start'], date_range['sql_end']))
    manual_dwell = load_manual_dwell(conn)
    conn.close()

    if df.empty: return render_template('analysis.html', weight_data='null', scatter_data='null', machine_data='null', dwell_data='null', freq_data='null', dwell_pending=0, date_range=date_range)

    df['dt'] = pd.to_datetime(df['timestamp'], format='%Y-%m-%d %H:%M:%S')

    # 1. Weight Chart
    weight_data = {"datasets": []}
    weight_df = df[df['weight'] > 0.5].copy()
    for cat in df['cat_identity'].unique():
        if cat in ['Unknown', 'System']: continue
        cat_df = weight_df[weight_df['cat_identity'] == cat]
        if cat_df.empty: continue
        
        data_points = [{'x': str(t).replace(" ", "T"), 'y': w} for t, w in zip(cat_df['timestamp'], cat_df['weight'])]
        weight_data["datasets"].append({
            "label": cat, 
            "data": data_points, 
            "borderColor": colors.get(cat, "#333"), 
            "backgroundColor": colors.get(cat, "#333"), 
            "tension": 0.3, "fill": False
        })

    # 2. Scatter
    scatter_data = {"datasets": []}
    for cat in df['cat_identity'].unique():
        if cat == 'System': continue
        cat_df = df[df['cat_identity'] == cat]
        points = []
        for _, row in cat_df.iterrows():
            if 'weight recorded' in str(row['activity']).lower(): continue
            try:
                parts = str(row['time']).split(':')
                decimal_time = int(parts[0]) + (int(parts[1]) / 60)
                points.append({'x': str(row['timestamp']).replace(" ", "T"), 'y': decimal_time})
            except: pass
        if points: 
            scatter_data["datasets"].append({"label": cat, "data": points, "backgroundColor": colors.get(cat, "#333")})

    # 3. Machine (Cycle Time)
    machine_health = []
    cycle_start = df[df['activity'] == 'Clean Cycle In Progress']
    cycle_end = df[df['activity'] == 'Clean Cycle Complete']
    for _, end_row in cycle_end.iterrows():
        start_candidates = cycle_start[cycle_start['dt'] < end_row['dt']]
        if start_candidates.empty: continue
        start_row = start_candidates.iloc[-1]
        duration_sec = (end_row['dt'] - start_row['dt']).total_seconds()
        mask = (df['dt'] > start_row['dt']) & (df['dt'] < end_row['dt']) & (df['activity'] == 'Cycle interrupted')
        if df[mask].empty and 60 < duration_sec < 300:
            machine_health.append({'x': str(start_row['timestamp']).replace(" ", "T"), 'y': round(duration_sec / 60, 2)})
            
    machine_data = {"datasets": [{"label": "Cycle Duration (min)", "data": machine_health, "borderColor": "#ffcd56", "backgroundColor": "#ffcd56"}]}

    # 4. Dwell Time (calculated, or manually entered when the CSV can't support it)
    dwell_data = {"datasets": []}
    dwell = compute_dwell_times(df, manual_dwell)
    dwell_pending = sum(1 for d in dwell if d['status'] == 'needs_input')
    dwell_points = {}
    for d in dwell:
        if d['status'] in ('calculated', 'manual'):
            dwell_points.setdefault(d['cat'], []).append({'x': str(d['cycle_timestamp']).replace(" ", "T"), 'y': d['minutes']})
    for cat, points in dwell_points.items():
        dwell_data["datasets"].append({"label": cat, "data": points, "backgroundColor": colors.get(cat, "#333")})

    # 5. Frequency
    freq_data = {"labels": [], "datasets": []}
    df['date_str'] = df['dt'].dt.strftime('%Y-%m-%d')
    days = sorted(df['date_str'].unique())
    freq_data["labels"] = days
    
    for cat in colors.keys():
        if cat == 'System': continue
        daily_counts = []
        for day in days:
            day_log = df[(df['date_str'] == day) & (df['cat_identity'] == cat)].sort_values('dt')
            visits = 0; last_time = None
            for _, row in day_log.iterrows():
                if last_time is None: visits += 1; last_time = row['dt']
                elif (row['dt'] - last_time).total_seconds() / 60 > 10: visits += 1; last_time = row['dt']
            daily_counts.append(visits)
        
        if sum(daily_counts) > 0:
            freq_data["datasets"].append({"label": cat, "data": daily_counts, "backgroundColor": colors.get(cat, "#333")})

    return render_template('analysis.html', weight_data=js_json(weight_data), scatter_data=js_json(scatter_data), machine_data=js_json(machine_data), dwell_data=js_json(dwell_data), freq_data=js_json(freq_data), dwell_pending=dwell_pending, date_range=date_range)

@app.route('/dwell', methods=['GET', 'POST'])
def dwell():
    conn = get_db()

    if request.method == 'POST':
        cycle_ts = request.form.get('cycle_timestamp', '')
        action = request.form.get('action')
        if action == 'clear':
            conn.execute("DELETE FROM dwell_manual WHERE cycle_timestamp = ?", (cycle_ts,))
            flash("Cleared manual dwell time.", "success")
        elif action == 'ignore':
            conn.execute("INSERT OR REPLACE INTO dwell_manual (cycle_timestamp, minutes) VALUES (?, NULL)", (cycle_ts,))
            flash("Cycle ignored for dwell time.", "warning")
        else:
            try:
                minutes = float(request.form.get('minutes', ''))
                if not 0 <= minutes <= 60: raise ValueError
                conn.execute("INSERT OR REPLACE INTO dwell_manual (cycle_timestamp, minutes) VALUES (?, ?)", (cycle_ts, minutes))
                flash(f"Saved {minutes:g} min.", "success")
            except ValueError:
                flash("Enter a dwell time between 0 and 60 minutes.", "error")
        conn.commit()
        conn.close()
        return redirect(url_for('dwell'))

    one_year_ago = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
    df = pd.read_sql_query("SELECT * FROM usage_logs WHERE cat_identity != 'Error' AND timestamp >= ? ORDER BY timestamp ASC", conn, params=(one_year_ago,))
    manual = load_manual_dwell(conn)
    conn.close()

    entries = []
    if not df.empty:
        df['dt'] = pd.to_datetime(df['timestamp'], format='%Y-%m-%d %H:%M:%S')
        entries = compute_dwell_times(df, manual)
    entries.reverse()  # Newest first
    return render_template('dwell.html',
                           pending=[e for e in entries if e['status'] == 'needs_input'],
                           overridden=[e for e in entries if e['status'] in ('manual', 'ignored')],
                           wait_minutes=CLEAN_CYCLE_WAIT_MINUTES, max_minutes=DWELL_MAX_MINUTES)

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files: return redirect(url_for('dashboard'))
    file = request.files['file']
    if file.filename == '': return redirect(url_for('dashboard'))

    conn = get_db()

    # --- 1. FORCE PROFILE CHECK ---
    # We check if any cats exist. If 0, stop the upload.
    cat_count = conn.execute("SELECT COUNT(*) FROM cat_profiles").fetchone()[0]
    
    if cat_count == 0:
        conn.close()
        flash("⚠️ You must add a Cat Profile before uploading data!", "error")
        return redirect(url_for('dashboard'))
    # --------------------------------

    added = 0
    skipped = 0

    # The CSV has no years, so anchor them to the export date in the filename
    # (falls back to the server clock). Two days of slack covers the export
    # finishing late in the day and UTC timestamps running ahead of local time.
    export_date = export_date_from_filename(file.filename)
    if export_date:
        latest_allowed = export_date + timedelta(days=2)
    else:
        latest_allowed = datetime.now() + timedelta(days=1)

    # --- 2. LOAD DATA FOR PROCESSING ---
    profile_rows = conn.execute("SELECT * FROM cat_profiles").fetchall()
    profiles = [dict(row) for row in profile_rows]

    # Blacklist matches on minute + weight + activity, so seconds added to
    # same-minute rows (see below) don't let blacklisted rows back in
    bl_rows = conn.execute("SELECT timestamp, weight, reason FROM data_blacklist").fetchall()
    blacklist_set = {(r['timestamp'][:16], float(r['weight']), r['reason']) for r in bl_rows}

    try:
        # Read in memory: never write a file named by the client to disk
        with io.StringIO(file.read().decode('utf-8-sig')) as f:
            next(f, None) 
            reader = csv.reader(f)
            parsed_rows = []

            for row in reader:
                if not row or len(row) < 3: continue
                raw_activity, raw_ts, raw_val = row[0].strip(), row[1].strip(), row[2].strip()

                try:
                    dt = csv_time_to_local(parse_whisker_timestamp(raw_ts, latest_allowed))

                    weight = 0.0
                    if 'lbs' in raw_val:
                         weight = float(raw_val.replace('lbs', '').strip())
                except (ValueError, IndexError):
                    skipped += 1
                    continue

                ts_str = dt.strftime('%Y-%m-%d %H:%M:%S')

                if (ts_str[:16], weight, raw_activity) in blacklist_set: continue

                parsed_rows.append({'dt': dt, 'timestamp': ts_str, 'date': dt.strftime('%Y-%m-%d'), 'time': dt.strftime('%H:%M:%S'), 'activity': raw_activity, 'weight': weight, 'raw_val': raw_val})

            # The CSV is newest-first, so reverse it before the (stable) sort to
            # keep same-minute events in the order they happened
            parsed_rows.reverse()
            parsed_rows.sort(key=lambda x: x['dt'])

            # Timestamps only have minute precision, but several events often
            # share a minute (e.g. "Cat detected" + "Weight recorded"). Skip rows
            # already in the DB by matching minute + activity + weight, and give
            # new rows the next free second so the timestamp key stays unique.
            existing_counts = Counter()
            taken_ts = set()
            if parsed_rows:
                existing = conn.execute("SELECT timestamp, activity, weight FROM usage_logs WHERE timestamp >= ? AND timestamp <= ?",
                                        (parsed_rows[0]['timestamp'][:16], parsed_rows[-1]['timestamp'][:16] + ':59')).fetchall()
                for r in existing:
                    existing_counts[(r['timestamp'][:16], r['activity'], float(r['weight']))] += 1
                    taken_ts.add(r['timestamp'])
            file_counts = Counter()

            # --- 3. INSERT WITH DYNAMIC CLASSIFICATION ---
            for i, row in enumerate(parsed_rows):
                cat_id, reason = classify_row(row, profiles)
                
                # Look-ahead logic
                if 'cat detected' in row['activity'].lower():
                    for j in range(i + 1, min(i + 20, len(parsed_rows))):
                        future_row = parsed_rows[j]
                        time_diff = (future_row['dt'] - row['dt']).total_seconds() / 60
                        if time_diff > 7: break
                        if 'weight recorded' in future_row['activity'].lower() and future_row['weight'] > 0.5:
                            cat_id, _ = classify_row(future_row, profiles)
                            reason = f"Matched w/ {future_row['weight']}lbs (+{int(time_diff)}m)"
                            break
                    if cat_id == 'Unknown': reason = "No weight found in 7m"

                minute = row['timestamp'][:16]
                key = (minute, row['activity'], row['weight'])
                file_counts[key] += 1
                if file_counts[key] <= existing_counts[key]: continue  # Already imported

                free_seconds = [s for s in range(60) if f"{minute}:{s:02d}" not in taken_ts]
                if not free_seconds: continue
                ts = f"{minute}:{free_seconds[0]:02d}"
                taken_ts.add(ts)

                conn.execute('INSERT INTO usage_logs (timestamp, date, time, weight, activity, metadata, cat_identity, flag_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', 
                    (ts, row['date'], ts[11:], row['weight'], row['activity'], json.dumps({'raw_val': row['raw_val']}), cat_id, reason))
                added += 1

            conn.execute('INSERT INTO upload_history (upload_date, filename, entries_added) VALUES (?, ?, ?)', (datetime.now().strftime('%Y-%m-%d %H:%M'), file.filename, added))
            
            # Commit and Close BEFORE backing up
            conn.commit()
            conn.close()

            # --- 4. AUTOMATIC BACKUP ---
            try:
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                backup_name = f"history_backup_{timestamp}.db"
                backup_path = os.path.join(BACKUP_FOLDER, backup_name)
                
                if not os.path.exists(BACKUP_FOLDER):
                    os.makedirs(BACKUP_FOLDER)
                    
                shutil.copy2(DB_NAME, backup_path)
                print(f"✅ Backup created: {backup_name}")
            except Exception as e:
                print(f"⚠️ Backup failed: {e}")
            # ---------------------------

            msg = f"Upload Successful! Added {added} records"
            if parsed_rows:
                msg += f" ({parsed_rows[0]['date']} to {parsed_rows[-1]['date']})"
            if skipped:
                msg += f", skipped {skipped} unreadable rows"
            flash(msg + ". (Backup created)", "success")

    except Exception as e:
        flash(f"Error: {e}", "error")
            
    return redirect(url_for('dashboard'))

# Missing routes like report, editor, uploads, etc. should be kept as is from your original code 
# (omitted here for brevity, but they need to exist)
@app.route('/uploads')
def uploads():
    conn = get_db()
    history = conn.execute("SELECT * FROM upload_history ORDER BY upload_date DESC").fetchall()
    conn.close()
    return render_template('uploads.html', history=history)

@app.route('/editor')
def editor():
    conn = get_db()
    
    # 1. Fetch Profiles (REQUIRED for dynamic buttons)
    profiles = conn.execute("SELECT * FROM cat_profiles").fetchall()

    # 2. Determine Current Target Date
    date_param = request.args.get('date')
    if date_param:
        current_date = date_param
    else:
        recent = conn.execute("SELECT date FROM usage_logs ORDER BY timestamp DESC LIMIT 1").fetchone()
        current_date = recent['date'] if recent else datetime.now().strftime('%Y-%m-%d')

    # 3. Fetch Data for Current Date
    valid_rows = conn.execute("SELECT * FROM usage_logs WHERE date = ? ORDER BY timestamp DESC", (current_date,)).fetchall()
    combined_logs = [dict(row) for row in valid_rows]
    
    # Add Blacklist entries
    bl_rows = conn.execute("SELECT * FROM data_blacklist WHERE timestamp LIKE ? ORDER BY timestamp DESC", (f"{current_date}%",)).fetchall()
    for r in bl_rows:
        combined_logs.append({
            'timestamp': r['timestamp'], 
            'date': current_date, 
            'time': 'Unknown',
            'weight': r['weight'], 
            'activity': f"{r['reason']} [Blacklisted]", 
            'cat_identity': 'Blacklisted'
        })
    
    combined_logs.sort(key=lambda x: x['timestamp'], reverse=True)

    # 4. Smart Navigation
    prev_row = conn.execute("SELECT date FROM usage_logs WHERE date < ? ORDER BY date DESC LIMIT 1", (current_date,)).fetchone()
    next_row = conn.execute("SELECT date FROM usage_logs WHERE date > ? ORDER BY date ASC LIMIT 1", (current_date,)).fetchone()
    
    prev_date = prev_row['date'] if prev_row else (datetime.strptime(current_date, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')
    next_date = next_row['date'] if next_row else (datetime.strptime(current_date, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')

    conn.close()

    return render_template('editor.html', 
                           logs=combined_logs, 
                           profiles=profiles,    # <--- PASS PROFILES TO TEMPLATE
                           current_date=current_date, 
                           prev_date=prev_date, 
                           next_date=next_date)

@app.route('/report')
def report():
    cat_id = request.args.get('cat', 'Cat_A')
    conn = get_db()
    
    # 1. Fetch Profile (For Birthday & Color)
    profile = conn.execute("SELECT * FROM cat_profiles WHERE name = ?", (cat_id,)).fetchone()
    cat_color = profile['color_hex'] if profile else "#333"
    
    # 2. Calculate Age
    age_str = "N/A"
    if profile and profile['birthday']:
        try:
            bday = datetime.strptime(profile['birthday'], '%Y-%m-%d')
            today = datetime.now()
            years = today.year - bday.year - ((today.month, today.day) < (bday.month, bday.day))
            months = (today.year - bday.year) * 12 + today.month - bday.month
            if years > 0: age_str = f"{years} yr {months % 12} mo"
            else: age_str = f"{months} months"
        except: pass

    # 3. Fetch Data for the selected period
    date_range = date_range_from_args(request.args, default='90')
    df = pd.read_sql_query("SELECT * FROM usage_logs WHERE cat_identity = ? AND timestamp >= ? AND timestamp < ? ORDER BY timestamp ASC",
                           conn, params=(cat_id, date_range['sql_start'], date_range['sql_end']))
    conn.close()

    df['dt'] = pd.to_datetime(df['timestamp'], format='%Y-%m-%d %H:%M:%S')
    
    # 4. Stats Calculation
    stats = {"current_weight": "N/A", "avg_visits": "0", "age": age_str}
    
    # Weight
    valid_weights = df[df['weight'] > 0.5]
    if not valid_weights.empty:
        stats["current_weight"] = f"{valid_weights.iloc[-1]['weight']} lbs"

    # Visits per day over the selected period
    recent_df = df.copy()
    
    daily_visits = {}
    if not recent_df.empty:
        recent_df['date_str'] = recent_df['dt'].dt.strftime('%Y-%m-%d')
        days = sorted(recent_df['date_str'].unique())
        total_visits = 0
        for day in days:
            day_log = recent_df[recent_df['date_str'] == day].sort_values('dt')
            visits = 0; last_time = None
            for _, row in day_log.iterrows():
                if last_time is None: visits += 1; last_time = row['dt']
                elif (row['dt'] - last_time).total_seconds() / 60 > 10: visits += 1; last_time = row['dt']
            daily_visits[day] = visits
            total_visits += visits
        
        # Average over the days covered: from the period start (or first record, if later) to the period end
        first_day = max(date_range['start'], days[0]) if date_range['start'] else days[0]
        days_tracked = (datetime.strptime(date_range['end'], '%Y-%m-%d') - datetime.strptime(first_day, '%Y-%m-%d')).days + 1
        stats["avg_visits"] = round(total_visits / max(1, days_tracked), 1)

    # 5. Chart Prep
    weight_data = [{'x': str(row['timestamp']).replace(" ", "T"), 'y': row['weight']} for _, row in valid_weights.iterrows()]
    freq_labels = list(daily_visits.keys())
    freq_values = list(daily_visits.values())
    flags = [f"⚠️ {day}: High frequency ({count} visits)" for day, count in daily_visits.items() if count > 8]

    return render_template('report.html', 
                           cat=cat_id, 
                           cat_color=cat_color,
                           stats=stats, 
                           weight_data=js_json(weight_data), 
                           freq_labels=js_json(freq_labels), 
                           freq_values=js_json(freq_values), 
                           flags=flags, 
                           has_data=not df.empty,
                           date_range=date_range,
                           generated_date=datetime.now().strftime('%b %d, %Y'))

if __name__ == '__main__':
    init_db()
    # Use the PORT from .env, or fallback to 5000 if not found
    port = int(os.environ.get('PORT', 5000)) 
    app.run(host='0.0.0.0', port=port)