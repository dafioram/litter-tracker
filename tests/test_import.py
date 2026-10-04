import sqlite3

import app as app_module

# Excerpt of a real Whisker export (newest first), with several minutes
# that contain more than one event
CSV = """Activity,Timestamp,Value
Clean Cycle Complete,10/4 7:01 am,-
Cycle interrupted,10/4 6:58 am,-
Clean Cycle In Progress,10/4 6:58 am,-
Weight recorded,10/4 6:51 am,9.6 lbs
Weight recorded,10/4 6:44 am,9.3 lbs
Cat detected,10/4 6:43 am,-
Clean Cycle Complete,10/2 10:01 am,-
Clean Cycle In Progress,10/2 9:58 am,-
Weight recorded,10/2 9:51 am,9.5 lbs
Cat detected,10/2 9:51 am,-
"""


def test_same_minute_rows_are_all_imported(upload, db):
    upload(CSV)
    rows = db("SELECT timestamp, activity FROM usage_logs ORDER BY timestamp")
    assert len(rows) == 10
    # Same-minute events keep their real order via the seconds field
    assert [(r['timestamp'], r['activity']) for r in rows if r['timestamp'].startswith('2026-10-04 06:58')] == [
        ('2026-10-04 06:58:00', 'Clean Cycle In Progress'),
        ('2026-10-04 06:58:01', 'Cycle interrupted'),
    ]
    assert [r['activity'] for r in rows if r['timestamp'].startswith('2026-10-02 09:51')] == ['Cat detected', 'Weight recorded']


def test_reimport_adds_nothing(upload, db):
    upload(CSV)
    response = upload(CSV)
    assert 'Added 0 records' in response.get_data(as_text=True)
    assert db("SELECT COUNT(*) FROM usage_logs")[0][0] == 10


def test_overlapping_import_adds_only_new_rows(upload, db):
    upload(CSV)
    newer = CSV.replace("Activity,Timestamp,Value\n", "Activity,Timestamp,Value\nWeight recorded,10/4 8:00 pm,9.4 lbs\n")
    response = upload(newer)
    assert 'Added 1 records' in response.get_data(as_text=True)


def test_fills_in_rows_dropped_by_older_imports(upload, db):
    # Older versions stored only one row per minute, at :00
    conn = sqlite3.connect(app_module.DB_NAME)
    conn.execute("INSERT INTO usage_logs (timestamp, date, time, weight, activity, metadata, cat_identity, flag_reason) "
                 "VALUES ('2026-10-04 06:58:00', '2026-10-04', '06:58:00', 0.0, 'Cycle interrupted', '{}', 'System', 'Machine Operation')")
    conn.commit(); conn.close()

    upload(CSV)
    rows = db("SELECT timestamp, activity FROM usage_logs WHERE timestamp LIKE '2026-10-04 06:58%' ORDER BY timestamp")
    assert [(r['timestamp'], r['activity']) for r in rows] == [
        ('2026-10-04 06:58:00', 'Cycle interrupted'),
        ('2026-10-04 06:58:01', 'Clean Cycle In Progress'),
    ]
    assert db("SELECT COUNT(*) FROM usage_logs")[0][0] == 10


def test_blacklisted_row_is_not_reimported(client, upload, db):
    upload(CSV)
    ts = db("SELECT timestamp FROM usage_logs WHERE activity = 'Cat detected' AND timestamp LIKE '2026-10-02 09:51%'")[0]['timestamp']
    client.get(f'/fix/{ts}/blacklist')
    upload(CSV)
    assert not db("SELECT * FROM usage_logs WHERE activity = 'Cat detected' AND timestamp LIKE '2026-10-02 09:51%'")
    # The weight in the same minute is untouched
    assert db("SELECT * FROM usage_logs WHERE activity = 'Weight recorded' AND timestamp LIKE '2026-10-02 09:51%'")
