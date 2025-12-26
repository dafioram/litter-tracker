import sqlite3
import pandas as pd
import os
import shutil
import tempfile
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)

# SECURITY: Load secret key from .env, or use a random default for dev
# This prevents your actual secret key from being hardcoded on GitHub
app.secret_key = os.environ.get('SECRET_KEY', 'dev_key_change_in_production')

# TIMEZONE: Get offset from .env (Default to 5 hours for EST)
# Users in other zones can change this variable
TIMEZONE_OFFSET = int(os.environ.get('TIMEZONE_OFFSET', 5))

# --- CONFIGURATION ---
DB_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
if not os.path.exists(DB_FOLDER):
    os.makedirs(DB_FOLDER)
DB_NAME = os.path.join(DB_FOLDER, 'litter_history.db')
BACKUP_FOLDER = os.path.join(DB_FOLDER, 'backups')

# Tolerance for classification (lbs)
WEIGHT_TOLERANCE = 2.0 

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
    
    # MIGRATION CHECK: If you already created the table without birthday, add it now
    try:
        conn.execute("ALTER TABLE cat_profiles ADD COLUMN birthday TEXT")
    except sqlite3.OperationalError:
        pass # Column likely already exists
    
    conn.commit()
    conn.close()

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

# --- ROUTES ---

@app.route('/')
def dashboard():
    init_db()
    conn = get_db()
    profiles = conn.execute("SELECT * FROM cat_profiles").fetchall()
    
    current_year_start = f"{datetime.now().year}-01-01"
    df = pd.read_sql_query(f"SELECT * FROM usage_logs WHERE timestamp >= '{current_year_start}' ORDER BY timestamp ASC", conn)
    
    thirty_days_ago_dt = datetime.now() - timedelta(days=30)
    cycle_count = 0; interrupt_count = 0; review_count = 0
    
    if not df.empty:
        cycle_count = conn.execute(f"SELECT COUNT(*) FROM usage_logs WHERE activity LIKE '%Clean Cycle%' AND timestamp > '{thirty_days_ago_dt}'").fetchone()[0]
        interrupt_count = conn.execute(f"SELECT COUNT(*) FROM usage_logs WHERE activity LIKE '%interrupted%' AND timestamp > '{thirty_days_ago_dt}'").fetchone()[0]
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
        name = request.form.get('name')
        weight = float(request.form.get('weight'))
        color = request.form.get('color')
        birthday = request.form.get('birthday') # <--- Get Birthday
        
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

@app.route('/fix/<path:timestamp_id>/<action>')
def fix_entry(timestamp_id, action):
    conn = get_db()
    
    # CLEAN THE ID: Remove leading/trailing spaces or newlines that breaks the DB lookup
    timestamp_id = timestamp_id.strip()
    
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

    # Dynamic Cat Assignment (Matches any string that isn't reserved keywords)
    else:
        conn.execute("UPDATE usage_logs SET cat_identity = ?, flag_reason = '' WHERE timestamp = ?", (action, timestamp_id))
        flash(f"Re-assigned to {action}", "success")
        
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
    
    current_year_start = f"{datetime.now().year}-01-01"
    df = pd.read_sql_query(f"SELECT * FROM usage_logs WHERE cat_identity != 'Error' AND timestamp >= '{current_year_start}' ORDER BY timestamp ASC", conn)
    conn.close()

    if df.empty: return render_template('analysis.html', weight_data=None, scatter_data=None, machine_data=None, dwell_data=None, freq_data=None)

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

    # 4. Dwell Time
    dwell_data = {"datasets": []}
    for cat in colors.keys():
        if cat == 'System': continue
        
        # Simple extraction for demo (Logic can be optimized)
        cat_points = []
        for _, start_row in cycle_start.iterrows():
            virtual_exit = start_row['dt'] - timedelta(minutes=15)
            window_start = virtual_exit - timedelta(minutes=30)
            candidates = df[(df['dt'] >= window_start) & (df['dt'] <= virtual_exit) & (df['activity'].str.contains('Cat detected', case=False))]
            if candidates.empty: continue
            last_cat = candidates.iloc[-1]
            
            # Match Identity
            if last_cat['cat_identity'] == cat:
                dwell_min = (virtual_exit - last_cat['dt']).total_seconds() / 60
                if 0 < dwell_min < 30:
                     cat_points.append({'x': str(start_row['timestamp']).replace(" ", "T"), 'y': round(dwell_min, 1)})
        
        if cat_points:
            dwell_data["datasets"].append({"label": cat, "data": cat_points, "backgroundColor": colors.get(cat, "#333")})

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

    def sanitize(obj): return json.loads(json.dumps(obj).replace('NaN', 'null'))
    return render_template('analysis.html', weight_data=json.dumps(sanitize(weight_data)), scatter_data=json.dumps(sanitize(scatter_data)), machine_data=json.dumps(sanitize(machine_data)), dwell_data=json.dumps(sanitize(dwell_data)), freq_data=json.dumps(sanitize(freq_data)))

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

    import tempfile, csv
    filepath = os.path.join(tempfile.gettempdir(), file.filename)
    file.save(filepath)
    
    added = 0
    current_year = datetime.now().year

    # --- 2. LOAD DATA FOR PROCESSING ---
    profile_rows = conn.execute("SELECT * FROM cat_profiles").fetchall()
    profiles = [dict(row) for row in profile_rows]

    bl_rows = conn.execute("SELECT timestamp, weight FROM data_blacklist").fetchall()
    blacklist_set = {f"{r['timestamp']}|{float(r['weight'])}" for r in bl_rows}

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            next(f, None) 
            reader = csv.reader(f)
            parsed_rows = []

            for row in reader:
                if not row or len(row) < 3: continue
                raw_activity, raw_ts, raw_val = row[0].strip(), row[1].strip(), row[2].strip()

                try:
                    parts = raw_ts.split()
                    month, day = map(int, parts[0].split('/'))
                    hour, minute = map(int, parts[1].split(':'))
                    if parts[2].lower() == 'pm' and hour != 12: hour += 12
                    elif parts[2].lower() == 'am' and hour == 12: hour = 0
                    
                    dt_utc = datetime(current_year, month, day, hour, minute)
                    # Use the Variable from .env (defined at top of app.py)
                    dt = dt_utc - timedelta(hours=TIMEZONE_OFFSET) 
                    
                    weight = 0.0
                    if 'lbs' in raw_val:
                         weight = float(raw_val.replace('lbs', '').strip())
                except: continue

                ts_str = dt.strftime('%Y-%m-%d %H:%M:%S')

                if f"{ts_str}|{weight}" in blacklist_set: continue

                parsed_rows.append({'dt': dt, 'timestamp': ts_str, 'date': dt.strftime('%Y-%m-%d'), 'time': dt.strftime('%H:%M:%S'), 'activity': raw_activity, 'weight': weight, 'raw_val': raw_val})

            parsed_rows.sort(key=lambda x: x['dt'])

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

                try:
                    conn.execute('INSERT INTO usage_logs (timestamp, date, time, weight, activity, metadata, cat_identity, flag_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', 
                        (row['timestamp'], row['date'], row['time'], row['weight'], row['activity'], json.dumps({'raw_val': row['raw_val']}), cat_id, reason))
                    added += 1
                except sqlite3.IntegrityError: pass

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

            flash(f"Upload Successful! Added {added} records. (Backup created)", "success")

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

    # 3. Fetch Data (Extended to 365 days to ensure data shows up)
    start_date = (datetime.now() - timedelta(days=365)).strftime('%Y-%m-%d')
    df = pd.read_sql_query(f"SELECT * FROM usage_logs WHERE cat_identity = '{cat_id}' AND timestamp >= '{start_date}' ORDER BY timestamp ASC", conn)
    conn.close()
    
    if df.empty: return f"<h3>No data found for {cat_id} in the last year.</h3>"

    df['dt'] = pd.to_datetime(df['timestamp'], format='%Y-%m-%d %H:%M:%S')
    
    # 4. Stats Calculation
    stats = {"current_weight": "N/A", "avg_visits": "0", "age": age_str}
    
    # Weight
    valid_weights = df[df['weight'] > 0.5]
    if not valid_weights.empty:
        stats["current_weight"] = f"{valid_weights.iloc[-1]['weight']} lbs"

    # Visits (Last 30 days only for frequency accuracy)
    thirty_days_ago = datetime.now() - timedelta(days=30)
    recent_df = df[df['dt'] >= thirty_days_ago].copy()
    
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
        
        days_tracked = max(1, (datetime.now() - recent_df.iloc[0]['dt']).days + 1)
        stats["avg_visits"] = round(total_visits / min(days_tracked, 30), 1)

    # 5. Chart Prep
    weight_data = [{'x': str(row['timestamp']).replace(" ", "T"), 'y': row['weight']} for _, row in valid_weights.iterrows()]
    freq_labels = list(daily_visits.keys())
    freq_values = list(daily_visits.values())
    flags = [f"⚠️ {day}: High frequency ({count} visits)" for day, count in daily_visits.items() if count > 8]

    return render_template('report.html', 
                           cat=cat_id, 
                           cat_color=cat_color,
                           stats=stats, 
                           weight_data=json.dumps(weight_data), 
                           freq_labels=json.dumps(freq_labels), 
                           freq_values=json.dumps(freq_values), 
                           flags=flags, 
                           generated_date=datetime.now().strftime('%b %d, %Y'))

if __name__ == '__main__':
    init_db()
    # Use the PORT from .env, or fallback to 5000 if not found
    port = int(os.environ.get('PORT', 5000)) 
    app.run(host='0.0.0.0', port=port)