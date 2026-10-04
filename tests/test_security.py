import base64

import app as app_module
from tests.test_import import CSV


def test_post_without_csrf_token_is_rejected(client, db):
    response = client.post('/manage_cats', data={'action': 'add', 'name': 'Mallory', 'weight': '9', 'color': '#000000'})
    assert response.status_code == 400
    assert not db("SELECT * FROM cat_profiles WHERE name = 'Mallory'")


def test_fix_requires_post(client):
    assert client.get('/fix').status_code == 405


def test_report_cat_name_is_not_sql_or_html(client):
    response = client.get('/report', query_string={'cat': "x' OR '1'='1"})
    assert response.status_code == 200
    assert 'No data found' in response.get_data(as_text=True)

    html = client.get('/report', query_string={'cat': '<script>alert(1)</script>'}).get_data(as_text=True)
    assert '<script>alert(1)' not in html


def test_cat_named_like_an_action_can_be_assigned(post, upload, db):
    post('/manage_cats', {'action': 'add', 'name': 'delete', 'weight': '12', 'color': '#ff0000'})
    upload(CSV)
    ts = db("SELECT timestamp FROM usage_logs WHERE activity = 'Weight recorded' LIMIT 1")[0]['timestamp']
    post('/fix', {'timestamp': ts, 'cat': 'delete'})
    assert db("SELECT cat_identity FROM usage_logs WHERE timestamp = ?", (ts,))[0][0] == 'delete'


def test_assigning_unknown_cat_is_refused(post, upload, db):
    upload(CSV)
    ts = db("SELECT timestamp FROM usage_logs WHERE activity = 'Weight recorded' LIMIT 1")[0]['timestamp']
    post('/fix', {'timestamp': ts, 'cat': 'Nobody'})
    assert db("SELECT cat_identity FROM usage_logs WHERE timestamp = ?", (ts,))[0][0] == 'Luna'


def test_invalid_cat_color_is_rejected(post, db):
    post('/manage_cats', {'action': 'add', 'name': 'Bad', 'weight': '9', 'color': 'red;}</style><script>'})
    assert not db("SELECT * FROM cat_profiles WHERE name = 'Bad'")


def test_cat_name_cannot_break_out_of_chart_script(post, upload, client):
    name = '</script><script>alert(1)</script>'
    post('/manage_cats', {'action': 'add', 'name': name, 'weight': '9.6', 'color': '#00ff00'})
    upload(CSV)
    for url in ['/', '/analysis']:
        assert name not in client.get(url).get_data(as_text=True)


def test_optional_password(client, monkeypatch):
    monkeypatch.setattr(app_module, 'APP_PASSWORD', 'hunter2')
    assert client.get('/').status_code == 401
    bad = base64.b64encode(b'admin:wrong').decode()
    assert client.get('/', headers={'Authorization': f'Basic {bad}'}).status_code == 401
    good = base64.b64encode(b'admin:hunter2').decode()
    assert client.get('/', headers={'Authorization': f'Basic {good}'}).status_code == 200
