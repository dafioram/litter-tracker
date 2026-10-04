import io
import sqlite3

import pytest

import app as app_module

CSRF = 'test-csrf-token'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, 'DB_NAME', str(tmp_path / 'test.db'))
    monkeypatch.setattr(app_module, 'BACKUP_FOLDER', str(tmp_path / 'backups'))
    monkeypatch.setattr(app_module, 'TIMEZONE_OFFSET', 0)
    monkeypatch.setattr(app_module, 'LOCAL_TZ', None)
    app_module.app.config['TESTING'] = True
    app_module.init_db()
    with app_module.app.test_client() as c:
        with c.session_transaction() as sess:
            sess['csrf_token'] = CSRF
        c.post('/manage_cats', data={'csrf_token': CSRF, 'action': 'add', 'name': 'Luna', 'weight': '9.5', 'color': '#36a2eb', 'birthday': ''})
        yield c


@pytest.fixture
def post(client):
    """POST a form with the CSRF token filled in."""
    def do_post(url, data):
        return client.post(url, data={'csrf_token': CSRF, **data}, follow_redirects=True)
    return do_post


@pytest.fixture
def db():
    def query(sql, params=()):
        conn = sqlite3.connect(app_module.DB_NAME)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows
    return query


@pytest.fixture
def upload(client):
    def do_upload(csv_text, filename='litter-robot_4_activity_2026-10-04.csv'):
        return client.post('/upload', data={'csrf_token': CSRF, 'file': (io.BytesIO(csv_text.encode()), filename)},
                           content_type='multipart/form-data', follow_redirects=True)
    return do_upload
