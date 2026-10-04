from datetime import date, timedelta

from werkzeug.datastructures import MultiDict

from app import date_range_from_args

TODAY = date.today()


def test_default_preset():
    r = date_range_from_args(MultiDict(), default='90')
    assert r['preset'] == '90'
    assert r['start'] == (TODAY - timedelta(days=90)).isoformat()
    assert r['end'] == TODAY.isoformat()
    assert r['sql_end'] == (TODAY + timedelta(days=1)).isoformat()


def test_all_time():
    r = date_range_from_args(MultiDict({'range': 'all'}))
    assert (r['start'], r['sql_start']) == ('', '0000-00-00')


def test_custom_range():
    r = date_range_from_args(MultiDict({'range': 'custom', 'start': '2026-09-01', 'end': '2026-09-30'}))
    assert (r['preset'], r['sql_start'], r['sql_end']) == ('custom', '2026-09-01', '2026-10-01')


def test_invalid_input_falls_back_to_default():
    assert date_range_from_args(MultiDict({'range': 'bogus'}), default='365')['preset'] == '365'
    assert date_range_from_args(MultiDict({'range': 'custom', 'start': 'x', 'end': ''}), default='30')['preset'] == '30'
    backwards = {'range': 'custom', 'start': '2026-09-30', 'end': '2026-09-01'}
    assert date_range_from_args(MultiDict(backwards), default='30')['preset'] == '30'


def test_report_uses_selected_period(client, upload):
    from tests.test_import import CSV  # Entries on 2026-10-02 and 2026-10-04
    upload(CSV)
    html = client.get('/report', query_string={'cat': 'Luna', 'range': 'custom', 'start': '2026-10-01', 'end': '2026-10-04'}).get_data(as_text=True)
    assert 'Period: 2026-10-01 to 2026-10-04' in html
    assert 'No data for' not in html
    assert '9.6 lbs' in html  # Latest weight in the period

    html = client.get('/report', query_string={'cat': 'Luna', 'range': 'custom', 'start': '2026-10-01', 'end': '2026-10-03'}).get_data(as_text=True)
    assert '9.5 lbs' in html  # 10/4 readings are outside the period

    html = client.get('/report', query_string={'cat': 'Luna', 'range': 'custom', 'start': '2025-01-01', 'end': '2025-01-31'}).get_data(as_text=True)
    assert 'No data for Luna in this period' in html


def test_analysis_accepts_range(client, upload):
    from tests.test_import import CSV
    upload(CSV)
    for args in [{'range': '30'}, {'range': 'all'}, {'range': 'custom', 'start': '2026-10-01', 'end': '2026-10-04'}]:
        assert client.get('/analysis', query_string=args).status_code == 200
