import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    os.environ['DATABASE_PATH'] = os.path.join(
        tempfile.mkdtemp(prefix='outlookEmail-external-account-tests-'), 'test.db',
    )

import web_outlook_app as web


HEADERS = {'X-API-Key': 'legacy-api-key'}
ACCOUNT = {
    'email': 'user@outlook.com', 'password': 'password-secret',
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'DATABASE', str(tmp_path / 'accounts.db'))
    monkeypatch.setitem(web.app.config, 'TESTING', True)
    monkeypatch.setitem(web.app.config, 'WTF_CSRF_ENABLED', True)
    monkeypatch.setattr(web, 'extract_graph_refresh_token', Mock(return_value={
        'success': True, 'client_id': 'client-id', 'refresh_token': 'initial-refresh-secret',
    }))
    monkeypatch.setattr(web, 'test_refresh_token', Mock(return_value=(True, None, 'refresh-secret', 'graph')))
    with web.app.app_context():
        web.init_db()
        web.set_setting('external_api_key', 'legacy-api-key')
    return web.app.test_client()


def register(client, **overrides):
    return client.post('/api/external/mail', headers=HEADERS, json={**ACCOUNT, **overrides})


def authorize_registered_account(client, **overrides):
    response = register(client, **overrides)
    assert response.status_code == 201
    return response.get_json()['account']['account_id']


def request_api(client, endpoint, headers=None, query=None):
    if endpoint == 'register':
        return client.post('/api/external/mail', headers=headers, query_string=query, json=ACCOUNT)
    return client.get('/api/external/latest-emails', headers=headers, query_string=query or {'email': ACCOUNT['email']})


@pytest.mark.parametrize('endpoint', ['register', 'latest'])
@pytest.mark.parametrize('headers', [
    {}, {'X-API-Key': 'wrong-key'}, {'X-API-Key': '   '},
    {'Authorization': 'Bearer legacy-api-key'}, {'Authorization': 'Basic legacy-api-key'},
])
def test_invalid_auth_rejected(client, endpoint, headers):
    response = request_api(client, endpoint, headers)
    assert response.status_code == 401
    assert response.headers['Cache-Control'] == 'no-store'
    with web.app.app_context():
        assert web.get_db().execute('SELECT COUNT(*) FROM accounts').fetchone()[0] == 0
        assert web.get_db().execute('SELECT COUNT(*) FROM outlook_upload_accounts').fetchone()[0] == 0


@pytest.mark.parametrize('endpoint', ['register', 'latest'])
def test_unconfigured_api_disabled(client, endpoint):
    with web.app.app_context():
        web.set_setting('external_api_key', '')
    assert request_api(client, endpoint, HEADERS).status_code == 403


def test_login_does_not_replace_api_key(client):
    with client.session_transaction() as session:
        session['logged_in'] = True
        session['login_session_version'] = web.DEFAULT_LOGIN_SESSION_VERSION
    assert request_api(client, 'register').status_code == 401


@pytest.mark.parametrize('key_parameter', ['api_key', 'apikey'])
def test_existing_query_key_authentication(client, key_parameter):
    response = request_api(client, 'register', query={key_parameter: 'legacy-api-key'})
    assert response.status_code == 201


def test_register_without_session_or_csrf_encrypts_credentials(client):
    response = register(client, email=' User@Outlook.COM ')
    assert response.status_code == 201
    payload = response.get_json()
    assert payload['account']['email'] == 'user@outlook.com'
    assert payload['account']['is_authorized'] is True
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'secret' not in response.get_data(as_text=True)
    with web.app.app_context():
        row = web.get_db().execute('SELECT * FROM outlook_upload_accounts').fetchone()
        assert row['password'] != ACCOUNT['password']
        assert web.decrypt_data(row['password']) == ACCOUNT['password']
        assert row['group_id'] == 1
        assert row['is_authorized'] == 1
        assert row['source'] == 'external_api'
        account = web.get_db().execute('SELECT * FROM accounts').fetchone()
        assert account['id'] == payload['account']['account_id']
        assert web.decrypt_data(account['password']) == ACCOUNT['password']
        assert account['refresh_token'] != 'refresh-secret'
        assert web.decrypt_data(account['refresh_token']) == 'refresh-secret'


def test_registration_accepts_custom_group(client):
    with web.app.app_context():
        db = web.get_db()
        cursor = db.execute("INSERT INTO groups (name) VALUES ('API group')")
        group_id = cursor.lastrowid
        db.commit()
    response = register(client, group_id=group_id)
    assert response.status_code == 201
    with web.app.app_context():
        account = web.get_db().execute('SELECT * FROM outlook_upload_accounts').fetchone()
        assert web.decrypt_data(account['password']) == ACCOUNT['password']
        assert account['group_id'] == group_id


def test_registration_preserves_password_whitespace(client):
    password = ' password-secret '
    assert register(client, password=password).status_code == 201
    with web.app.app_context():
        row = web.get_db().execute('SELECT password FROM outlook_upload_accounts').fetchone()
        assert web.decrypt_data(row['password']) == password


def test_old_post_route_removed(client):
    assert client.post('/api/external/accounts', headers=HEADERS, json=ACCOUNT).status_code == 405


def test_registration_finishes_authorization_before_response(client, monkeypatch):
    caller_thread = threading.get_ident()
    run_task = web.run_graph_oauth_task
    task_threads = []
    def run_synchronously(*args, **kwargs):
        task_threads.append(threading.get_ident())
        return run_task(*args, **kwargs)
    monkeypatch.setattr(web, 'run_graph_oauth_task', run_synchronously)
    response = register(client)
    assert response.status_code == 201
    assert task_threads == [caller_thread]
    assert response.get_json()['account']['authorization_type'] == 'graph'
    web.extract_graph_refresh_token.assert_called_once()
    assert web.extract_graph_refresh_token.call_args.args == (ACCOUNT['email'], ACCOUNT['password'])
    assert web.extract_graph_refresh_token.call_args.kwargs['scope'] == web.GRAPH_EXTRACT_GRAPH_SCOPE
    web.test_refresh_token.assert_called_once_with(
        'client-id', 'initial-refresh-secret', proxy_url='', authorization_type='graph',
    )
    with web.app.app_context():
        upload_row = web.get_upload_account_for_graph_auth(response.get_json()['account']['id'])
        assert upload_row['is_authorized'] == 1
        account = web.get_account_by_id(response.get_json()['account']['account_id'])
        assert account['email'] == ACCOUNT['email']
        assert account['password'] == ACCOUNT['password']
        assert account['refresh_token'] == 'refresh-secret'
        assert web.get_upload_account_for_graph_auth(upload_row['id'])['is_authorized'] == 1
    monkeypatch.setattr(web, 'get_emails_graph', Mock(return_value={'success': True, 'emails': []}))
    assert request_api(client, 'latest', HEADERS).status_code == 200


@pytest.mark.parametrize('phase', ['extract', 'validate', 'save'])
def test_authorization_failure_keeps_pending_registration(client, monkeypatch, phase):
    if phase == 'extract':
        monkeypatch.setattr(web, 'extract_graph_refresh_token', Mock(return_value={
            'success': False, 'error': 'password-secret', 'details': 'initial-refresh-secret',
        }))
    elif phase == 'validate':
        monkeypatch.setattr(web, 'test_refresh_token', Mock(return_value=(False, 'refresh-secret', '', 'graph')))
    else:
        mark_authorized = web.mark_upload_account_authorized
        def fail_during_save(account_id):
            mark_authorized(account_id)
            raise RuntimeError('password-secret')
        monkeypatch.setattr(web, 'mark_upload_account_authorized', fail_during_save)
    response = register(client)
    assert response.status_code == 502
    payload = response.get_json()
    assert payload['success'] is False
    assert payload['account']['is_authorized'] is False
    assert 'account_id' not in payload['account']
    assert 'secret' not in response.get_data(as_text=True)
    with web.app.app_context():
        row = web.get_upload_account_for_graph_auth(payload['account']['id'])
        assert row['is_authorized'] == 0
        assert web.get_upload_account_plain_password(row) == ACCOUNT['password']
        assert web.get_account_by_email(ACCOUNT['email']) is None
    assert register(client).status_code == 409


def test_oauth_runs_after_releasing_registration_write_lock(client, monkeypatch):
    def extract(*args, **kwargs):
        db = web.get_db()
        db.execute("INSERT INTO groups (name) VALUES ('OAuth writer')")
        db.commit()
        return {'success': True, 'client_id': 'client-id', 'refresh_token': 'initial-refresh-secret'}
    monkeypatch.setattr(web, 'extract_graph_refresh_token', extract)
    assert register(client).status_code == 201


def test_missing_oauth_completion_is_not_success(client, monkeypatch):
    monkeypatch.setattr(web, 'run_graph_oauth_task', Mock())
    response = register(client)
    assert response.status_code == 502
    assert response.get_json()['account']['is_authorized'] is False


def test_worker_exception_returns_failure(client, monkeypatch):
    monkeypatch.setattr(web, 'run_graph_oauth_task', Mock(side_effect=RuntimeError('password-secret')))
    response = register(client)
    assert response.status_code == 502
    assert 'password-secret' not in response.get_data(as_text=True)


def test_actual_validation_channel_is_returned(client, monkeypatch):
    monkeypatch.setattr(web, 'test_refresh_token', Mock(return_value=(True, None, 'refresh-secret', 'imap')))
    response = register(client)
    assert response.status_code == 201
    assert response.get_json()['account']['authorization_type'] == 'imap'


@pytest.mark.parametrize('data', [
    [], None, 'invalid', {}, {**ACCOUNT, 'email': 'invalid'},
    {**ACCOUNT, 'email': 'two@@outlook.com'}, {**ACCOUNT, 'email': 'user @outlook.com'},
    {**ACCOUNT, 'email': 123}, {**ACCOUNT, 'email': 'x' * 250 + '@outlook.com'},
    {'email': ACCOUNT['email']}, {**ACCOUNT, 'password': ''},
    {**ACCOUNT, 'password': '  '}, {**ACCOUNT, 'password': None},
    {**ACCOUNT, 'password': 123}, {**ACCOUNT, 'client_id': 'client-id'},
    {**ACCOUNT, 'refresh_token': 'refresh-secret'},
    {**ACCOUNT, 'remark': {}}, {**ACCOUNT, 'group_id': True},
    {**ACCOUNT, 'group_id': 1.5}, {**ACCOUNT, 'group_id': '1'},
    {**ACCOUNT, 'group_id': -1}, {**ACCOUNT, 'group_id': 9999},
    {**ACCOUNT, 'provider': 'custom'},
])
def test_invalid_registration_does_not_write(client, data):
    assert client.post('/api/external/mail', headers=HEADERS, json=data).status_code == 400
    with web.app.app_context():
        assert web.get_db().execute('SELECT COUNT(*) FROM accounts').fetchone()[0] == 0
        assert web.get_db().execute('SELECT COUNT(*) FROM outlook_upload_accounts').fetchone()[0] == 0


def test_case_insensitive_duplicate_preserves_credentials(client):
    assert register(client).status_code == 201
    assert register(client, email='USER@OUTLOOK.COM', password='replacement').status_code == 409
    with web.app.app_context():
        row = web.get_db().execute('SELECT * FROM outlook_upload_accounts').fetchone()
        assert web.decrypt_data(row['password']) == ACCOUNT['password']
        assert row['is_authorized'] == 1
        assert web.get_db().execute('SELECT COUNT(*) FROM outlook_upload_accounts').fetchone()[0] == 1


def test_existing_authorized_account_cannot_be_registered(client):
    with web.app.app_context():
        assert web.add_account('USER@OUTLOOK.COM', 'original', 'client-id', 'refresh-secret')
    assert register(client).status_code == 409
    with web.app.app_context():
        assert web.get_account_by_email(ACCOUNT['email'])['password'] == 'original'
        assert web.get_db().execute('SELECT COUNT(*) FROM outlook_upload_accounts').fetchone()[0] == 0


def test_existing_alias_cannot_be_registered(client):
    account_id = authorize_registered_account(client)
    with web.app.app_context():
        db = web.get_db()
        db.execute('INSERT INTO account_aliases (account_id, alias_email) VALUES (?, ?)',
                   (account_id, 'alias@outlook.com'))
        db.commit()
    assert register(client, email='ALIAS@OUTLOOK.COM').status_code == 409


def test_simultaneous_case_variants_only_register_once(client):
    def submit(address):
        return register(web.app.test_client(), email=address).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(submit, ['User@Outlook.com', 'USER@OUTLOOK.COM']))
    assert sorted(statuses) == [201, 409]


def test_latest_fetches_server_on_every_call(client, monkeypatch):
    authorize_registered_account(client)
    graph = Mock(side_effect=[
        {'success': True, 'emails': [{'id': 'first', 'subject': 'First'}]},
        {'success': True, 'emails': [{'id': 'second', 'subject': 'Second'}]},
    ])
    monkeypatch.setattr(web, 'get_emails_graph', graph)
    local = Mock(side_effect=AssertionError('Must not use local mail'))
    monkeypatch.setattr(web, 'fetch_retained_normal_mail_list', local)
    query = {'email': 'USER@OUTLOOK.COM', 'source': 'local', 'local_retention': 'true'}
    first = client.get('/api/external/latest-emails', headers=HEADERS, query_string=query)
    second = client.get('/api/external/latest-emails', headers=HEADERS, query_string=query)
    assert first.status_code == second.status_code == 200
    assert first.get_json()['emails'][0]['id'] == 'first'
    assert second.get_json()['emails'][0]['id'] == 'second'
    assert graph.call_count == 2
    assert graph.call_args.args[:5] == ('client-id', 'refresh-secret', 'inbox', 0, 1)
    local.assert_not_called()


def test_plus_address_and_top_folder_preserved(client, monkeypatch):
    authorize_registered_account(client, email='user+tag@outlook.com')
    fetch = Mock(return_value={'success': True, 'emails': [], 'has_more': False})
    monkeypatch.setattr(web, 'fetch_account_emails', fetch)
    response = client.get('/api/external/latest-emails?email=user+tag@outlook.com&top=50&folder=junkemail', headers=HEADERS)
    assert response.status_code == 200
    assert response.get_json()['emails'] == []
    assert response.get_json()['resolved_email'] == 'user+tag@outlook.com'
    assert fetch.call_args.args[1:] == ('junkemail', 0, 50)


@pytest.mark.parametrize('query', [
    {}, {'email': 'invalid'}, {'email': ACCOUNT['email'], 'folder': 'all'},
    *[{'email': ACCOUNT['email'], 'top': top} for top in ('0', '-1', '51', 'abc', '1.5', '')],
])
def test_invalid_mail_query_does_not_fetch(client, monkeypatch, query):
    fetch = Mock()
    monkeypatch.setattr(web, 'fetch_account_emails', fetch)
    assert client.get('/api/external/latest-emails', headers=HEADERS, query_string=query).status_code == 400
    fetch.assert_not_called()


def test_unknown_address(client, monkeypatch):
    fetch = Mock()
    monkeypatch.setattr(web, 'fetch_account_emails', fetch)
    assert request_api(client, 'latest', HEADERS).status_code == 404
    fetch.assert_not_called()


@pytest.mark.parametrize('raises', [True, False])
def test_upstream_failure_has_502_without_secrets(client, monkeypatch, raises):
    authorize_registered_account(client)
    fetch = Mock(side_effect=RuntimeError('refresh-secret')) if raises else Mock(return_value={
        'success': False, 'error': 'refresh-secret', 'details': {'token': 'refresh-secret'},
    })
    monkeypatch.setattr(web, 'fetch_account_emails', fetch)
    response = request_api(client, 'latest', HEADERS)
    assert response.status_code == 502
    assert 'refresh-secret' not in response.get_data(as_text=True)


def test_legacy_auth_and_management_protection_unchanged(client):
    assert register(client).status_code == 201
    assert client.get('/api/external/accounts').status_code == 401
    response = client.get('/api/external/accounts', headers={'X-API-Key': 'legacy-api-key'})
    assert response.status_code == 200
    assert response.get_json()['success'] is True
    assert client.get('/api/accounts', headers=HEADERS).status_code == 401
    assert client.post('/api/accounts', headers=HEADERS, json=ACCOUNT).status_code == 400
    assert getattr(web.app.view_functions['api_external_get_emails'], '_requires_api_key', False)
    assert getattr(web.app.view_functions['api_external_register_account'], '_requires_api_key', False)
    assert getattr(web.app.view_functions['api_external_latest_emails'], '_requires_api_key', False)


@pytest.mark.parametrize('phase', ['extract', 'validate'])
@pytest.mark.parametrize('collision', ['primary', 'alias'])
def test_registration_conflict_during_oauth_preserves_web_import(client, monkeypatch, phase, collision):
    oauth_waiting = threading.Event()
    resume_oauth = threading.Event()
    original_phase = getattr(web, 'extract_graph_refresh_token' if phase == 'extract' else 'test_refresh_token')

    def pause_oauth(*args, **kwargs):
        oauth_waiting.set()
        assert resume_oauth.wait(10), 'OAuth regression test did not resume'
        return original_phase(*args, **kwargs)

    monkeypatch.setattr(web, 'extract_graph_refresh_token' if phase == 'extract' else 'test_refresh_token', pause_oauth)
    ui_client = web.app.test_client()
    with ui_client.session_transaction() as session:
        session['logged_in'] = True
        session['login_session_version'] = web.DEFAULT_LOGIN_SESSION_VERSION
    csrf_token = ui_client.get('/api/csrf-token').get_json()['csrf_token']

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(register, web.app.test_client())
        try:
            assert oauth_waiting.wait(10), 'API did not reach the OAuth phase'
            imported_email = 'USER@OUTLOOK.COM' if collision == 'primary' else 'owner@outlook.com'
            imported = ui_client.post('/api/accounts', headers={'X-CSRFToken': csrf_token}, json={
                'account_string': f'{imported_email}----web-password----web-client----web-refresh',
                'group_id': 1, 'remark': 'Web import', 'status': 'inactive', 'proxy_url': 'direct',
            })
            assert imported.status_code == 200
            assert imported.get_json()['added_count'] == 1
            with web.app.app_context():
                db = web.get_db()
                before = dict(db.execute('SELECT * FROM accounts').fetchone())
                if collision == 'alias':
                    db.execute('INSERT INTO account_aliases (account_id, alias_email) VALUES (?, ?)',
                               (before['id'], 'USER@OUTLOOK.COM'))
                    db.commit()
        finally:
            resume_oauth.set()
        response = future.result(timeout=10)

    assert response.status_code == 409
    assert response.get_json() == {'success': False, 'error': 'Email account already exists'}
    assert response.headers['Cache-Control'] == 'no-store'
    with web.app.app_context():
        db = web.get_db()
        assert dict(db.execute('SELECT * FROM accounts').fetchone()) == before
        assert db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0] == 1
        assert db.execute('SELECT is_authorized FROM outlook_upload_accounts').fetchone()[0] == 0
