import pandas as pd

from app import compute_dwell_times


def logs(*events):
    """events: (time 'HH:MM', activity, cat) on 2026-10-04"""
    df = pd.DataFrame([{'timestamp': f'2026-10-04 {t}:00', 'activity': a, 'cat_identity': c,
                        'weight': 9.5 if a == 'Weight recorded' else 0.0} for t, a, c in events])
    df['dt'] = pd.to_datetime(df['timestamp'])
    return df


def test_dwell_is_exit_minus_entry():
    df = logs(('06:43', 'Cat detected', 'Luna'), ('06:45', 'Weight recorded', 'Luna'),
              ('06:52', 'Clean Cycle In Progress', 'System'), ('06:55', 'Clean Cycle Complete', 'System'))
    [d] = compute_dwell_times(df, {})
    # Wait is 7 min, so the cat left at 06:45: 2 minutes after entering
    assert (d['status'], d['minutes'], d['cat']) == ('calculated', 2.0, 'Luna')


def test_missing_cat_detected_needs_input():
    df = logs(('06:45', 'Weight recorded', 'Luna'), ('06:52', 'Clean Cycle In Progress', 'System'))
    [d] = compute_dwell_times(df, {})
    assert d['status'] == 'needs_input'
    assert d['calculated'] is None
    assert d['cat'] == 'Luna'  # Falls back to the weight reading's cat


def test_too_long_needs_input():
    df = logs(('06:30', 'Cat detected', 'Luna'), ('06:44', 'Weight recorded', 'Luna'),
              ('06:51', 'Clean Cycle In Progress', 'System'))
    [d] = compute_dwell_times(df, {})
    assert (d['status'], d['calculated'], d['minutes']) == ('needs_input', 14.0, None)


def test_manual_value_and_ignore_win():
    df = logs(('06:30', 'Cat detected', 'Luna'), ('06:51', 'Clean Cycle In Progress', 'System'),
              ('08:00', 'Cat detected', 'Luna'), ('08:08', 'Clean Cycle In Progress', 'System'))
    manual = {'2026-10-04 06:51:00': 3.5, '2026-10-04 08:08:00': None}
    first, second = compute_dwell_times(df, manual)
    assert (first['status'], first['minutes']) == ('manual', 3.5)
    assert (second['status'], second['minutes']) == ('ignored', None)


def test_detection_before_previous_cycle_is_not_reused():
    df = logs(('06:00', 'Cat detected', 'Luna'), ('06:01', 'Weight recorded', 'Luna'),
              ('06:08', 'Clean Cycle In Progress', 'System'),
              ('06:20', 'Clean Cycle In Progress', 'System'))
    first, second = compute_dwell_times(df, {})
    assert first['minutes'] == 1.0
    assert second['status'] == 'needs_input'


def test_dwell_page_save_ignore_clear(post, db):
    ts = '2026-10-04 06:51:00'
    post('/dwell', {'cycle_timestamp': ts, 'action': 'save', 'minutes': '4'})
    assert db("SELECT minutes FROM dwell_manual WHERE cycle_timestamp = ?", (ts,))[0][0] == 4.0
    post('/dwell', {'cycle_timestamp': ts, 'action': 'ignore'})
    assert db("SELECT minutes FROM dwell_manual WHERE cycle_timestamp = ?", (ts,))[0][0] is None
    post('/dwell', {'cycle_timestamp': ts, 'action': 'clear'})
    assert not db("SELECT * FROM dwell_manual")
    post('/dwell', {'cycle_timestamp': ts, 'action': 'save', 'minutes': 'abc'})
    assert not db("SELECT * FROM dwell_manual")


def test_dwell_and_analysis_pages_render(client, upload):
    from tests.test_import import CSV
    upload(CSV)
    assert client.get('/dwell').status_code == 200
    assert client.get('/analysis').status_code == 200
