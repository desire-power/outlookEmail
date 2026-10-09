import json
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


GRAPH_TOKEN_EXTRACTOR = web.extract_graph_refresh_token
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


HTTP_PROXY = 'http://proxy-user:proxy%2Bsecret%40word@proxy.example:8080'


def set_test_proxies(account_id=None):
    with web.app.app_context():
        db = web.get_db()
        table = 'accounts' if account_id else 'groups'
        db.execute(
            f'UPDATE {table} SET proxy_url = ?, fallback_proxy_url_1 = ?, fallback_proxy_url_2 = ? WHERE id = ?',
            ('http://stored.example:8080', 'http://fallback.example:8080', 'direct', account_id or 1),
        )
        db.commit()


@pytest.mark.parametrize('proxy', [HTTP_PROXY, 'http://[::1]:8080'])
def test_registration_http_proxy_used_for_oauth_and_saved(client, proxy):
    set_test_proxies()
    response = register(client, httpProxy=f' {proxy} ')
    assert response.status_code == 201
    assert web.extract_graph_refresh_token.call_args.kwargs['proxy_url'] == proxy
    assert web.test_refresh_token.call_args.kwargs['proxy_url'] == proxy
    with web.app.app_context():
        upload = web.get_upload_account_for_graph_auth(response.get_json()['account']['id'])
        account = web.get_account_by_id(response.get_json()['account']['account_id'])
        assert upload['proxy_url'] == account['proxy_url'] == proxy
    assert 'proxy-user' not in response.get_data(as_text=True)
    assert 'proxy%2Bsecret' not in response.get_data(as_text=True)


@pytest.mark.parametrize('proxy_input', [{}, {'httpProxy': ''}, {'httpProxy': '  '}])
def test_registration_omitted_or_empty_proxy_inherits_group(client, proxy_input):
    set_test_proxies()
    assert register(client, **proxy_input).status_code == 201
    assert web.extract_graph_refresh_token.call_args.kwargs['proxy_url'] == 'http://stored.example:8080'
    assert web.test_refresh_token.call_args.kwargs['proxy_url'] == 'http://stored.example:8080'


def test_registration_failure_keeps_http_proxy_without_exposing_it(client, monkeypatch):
    monkeypatch.setattr(web, 'extract_graph_refresh_token', Mock(return_value={
        'success': False, 'error': HTTP_PROXY,
    }))
    response = register(client, httpProxy=HTTP_PROXY)
    assert response.status_code == 502
    with web.app.app_context():
        row = web.get_upload_account_for_graph_auth(response.get_json()['account']['id'])
        assert row['proxy_url'] == HTTP_PROXY
        assert not row['is_authorized']
    assert HTTP_PROXY not in response.get_data(as_text=True)


@pytest.mark.parametrize('inherit', [True, False])
@pytest.mark.parametrize('channel', ['graph', 'imap', 'generic_imap'])
def test_latest_http_proxy_overrides_fetch_without_saving(client, monkeypatch, inherit, channel):
    account_id = authorize_registered_account(client)
    set_test_proxies(None if inherit else account_id)
    with web.app.app_context():
        if channel != 'graph':
            db = web.get_db()
            if channel == 'imap':
                db.execute("UPDATE accounts SET authorization_type = 'imap' WHERE id = ?", (account_id,))
            else:
                db.execute("UPDATE accounts SET account_type = 'imap', imap_host = 'imap.example' WHERE id = ?", (account_id,))
            db.commit()
        before = web.get_account_by_id(account_id)
    functions = {'graph': 'get_emails_graph', 'imap': 'get_emails_imap_with_server', 'generic_imap': 'get_emails_imap_generic'}
    fetch = Mock(return_value={'success': True, 'emails': []})
    monkeypatch.setattr(web, functions[channel], fetch)
    query = {'email': ACCOUNT['email'], 'httpProxy': HTTP_PROXY}
    response = client.get('/api/external/latest-emails', headers=HEADERS, query_string=query)
    assert response.status_code == 200
    fetch.assert_called_once()
    if channel == 'generic_imap':
        assert fetch.call_args.args[-1] == HTTP_PROXY
    else:
        assert fetch.call_args.args[-2:] == (HTTP_PROXY, ['', ''])
    with web.app.app_context():
        after = web.get_account_by_id(account_id)
        for field in ('proxy_url', 'fallback_proxy_url_1', 'fallback_proxy_url_2'):
            assert after[field] == before[field]
    assert HTTP_PROXY not in response.get_data(as_text=True)


@pytest.mark.parametrize('proxy_input', [{}, {'httpProxy': ''}, {'httpProxy': '  '}])
def test_latest_omitted_or_empty_proxy_keeps_existing_fallbacks(client, monkeypatch, proxy_input):
    authorize_registered_account(client)
    set_test_proxies()
    graph = Mock(return_value={'success': True, 'emails': []})
    monkeypatch.setattr(web, 'get_emails_graph', graph)
    query = {'email': ACCOUNT['email'], **proxy_input}
    assert client.get('/api/external/latest-emails', headers=HEADERS, query_string=query).status_code == 200
    assert graph.call_args.args[-2:] == ('http://stored.example:8080', ['http://fallback.example:8080', 'direct'])


@pytest.mark.parametrize('proxy', [
    'proxy.example:8080', 'socks5://proxy.example:1080', 'https://proxy.example:8080',
    'http://proxy.example', 'http://:8080', 'http://proxy.example:abc',
    'http://proxy.example:0', 'http://proxy.example:65536', 'http://[broken:8080',
    'http://proxy.example:8080/path', 'http://proxy.example:8080?secret=hidden',
    'http://proxy.example:8080#hidden', 'http://proxy\n.example:8080',
])
@pytest.mark.parametrize('endpoint', ['register', 'latest'])
def test_invalid_http_proxy_rejected_before_write_or_fetch(client, monkeypatch, proxy, endpoint):
    fetch = Mock()
    monkeypatch.setattr(web, 'fetch_account_emails', fetch)
    if endpoint == 'register':
        response = register(client, httpProxy=proxy)
    else:
        response = client.get('/api/external/latest-emails', headers=HEADERS,
                              query_string={'email': ACCOUNT['email'], 'httpProxy': proxy})
    assert response.status_code == 400
    assert proxy not in response.get_data(as_text=True)
    fetch.assert_not_called()
    web.extract_graph_refresh_token.assert_not_called()
    with web.app.app_context():
        assert web.get_db().execute('SELECT COUNT(*) FROM outlook_upload_accounts').fetchone()[0] == 0


@pytest.mark.parametrize('proxy', [None, False, 123, {}, []])
def test_registration_non_string_http_proxy_rejected(client, proxy):
    response = register(client, httpProxy=proxy)
    assert response.status_code == 400
    assert response.get_json()['error'] == 'httpProxy must be a string'
    web.extract_graph_refresh_token.assert_not_called()


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
            'success': False, 'error': 'password-secret', 'details': 'refresh_token=initial-refresh-secret',
        }))
    elif phase == 'validate':
        monkeypatch.setattr(web, 'test_refresh_token', Mock(return_value=(False, 'initial-refresh-secret', '', 'graph')))
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
    assert response.get_json()['rawErrorLog'] == '授权任务异常\n***'


@pytest.mark.parametrize('javascript_literal', [
    json.dumps('Your account or password is incorrect. If you don\'t remember your password, reset it.'),
    r"'Your account or password is incorrect. If you don\'t remember your password, reset it.'",
])
def test_registration_returns_full_graph_login_error_log(client, monkeypatch, javascript_literal):
    # Exercise the real extractor, task queue and route without contacting Microsoft.
    monkeypatch.setattr(web, 'extract_graph_refresh_token', GRAPH_TOKEN_EXTRACTOR)
    session = Mock()
    session.headers = {}
    session.get.return_value = web.make_light_response('https://login.live.com/', '<input name="PPFT" value="flow">')
    session.post.return_value = web.make_light_response(
        'https://login.live.com/ppsecure/post.srf', f'<script>var sErrTxt = {javascript_literal};</script>',
    )
    monkeypatch.setattr(web.requests, 'Session', Mock(return_value=session))
    response = register(client)
    assert response.status_code == 502
    assert response.headers['Cache-Control'] == 'no-store'
    payload = response.get_json()
    assert payload['error'] == 'OAuth authorization failed'
    assert payload['rawErrorLog'].splitlines() == [
        '开始 GraphAPI OAuth 授权',
        '授权模式: GraphAPI',
        f'授权 Scope: {web.GRAPH_EXTRACT_GRAPH_SCOPE}',
        f'获取 Microsoft 授权页面: {ACCOUNT["email"]}',
        '提交 Microsoft 登录凭据',
        'Microsoft 登录失败',
        "JavaScript错误信息: Your account or password is incorrect. If you don't remember your password, reset it.",
    ]
    web.test_refresh_token.assert_not_called()


def test_registration_preserves_long_multiline_diagnostics_and_request_scoped_log(client, monkeypatch):
    detail = 'AADSTS50076: ' + '追加の本人確認が必要です。' * 60 + '\nTrace ID: trace-123\nCorrelation ID: correlation-456'
    def fail_extraction(*args, log, **kwargs):
        log('Microsoft request started')
        return {'success': False, 'error': 'OAuth 错误', 'details': detail}
    extract = Mock(side_effect=fail_extraction)
    monkeypatch.setattr(web, 'extract_graph_refresh_token', extract)
    payload = register(client).get_json()
    assert payload['rawErrorLog'].endswith('Microsoft request started\nOAuth 错误\n' + detail)
    extract.side_effect = lambda *args, **kwargs: {'success': False, 'error': 'Second registration failed', 'details': 'Second attempt only'}
    retry = register(client, email='another@outlook.com').get_json()
    assert retry['account']['id'] != payload['account']['id']
    assert retry['rawErrorLog'].endswith('Second registration failed\nSecond attempt only')
    assert 'trace-123' not in retry['rawErrorLog']


@pytest.mark.parametrize('phase', ['validate', 'save'])
def test_registration_returns_validation_and_save_diagnostics_with_masked_secrets(client, monkeypatch, phase):
    if phase == 'validate':
        monkeypatch.setattr(web, 'test_refresh_token', Mock(return_value=(
            False, 'invalid_grant\nToken: initial-refresh-secret\nRotated: rotated-token-value', 'rotated-token-value', 'graph',
        )))
        expected = 'GraphAPI refresh_token 验证失败\ninvalid_grant\nToken: ***\nRotated: ***'
    else:
        monkeypatch.setattr(web, 'save_external_mail_authorization', Mock(side_effect=RuntimeError(
            'Database write failed: password-secret refresh-secret initial-refresh-secret',
        )))
        expected = '授权任务异常\nDatabase write failed: *** *** ***'
    response = register(client)
    assert response.status_code == 502
    assert response.get_json()['rawErrorLog'].endswith(expected)
    assert 'secret' not in response.get_data(as_text=True)
    assert 'rotated-token-value' not in response.get_data(as_text=True)


def test_registration_masks_password_with_special_characters_in_error_log(client, monkeypatch):
    password = 'pa$$ word&extra'
    monkeypatch.setattr(web, 'extract_graph_refresh_token', Mock(return_value={
        'success': False, 'error': 'Sign-in failed',
        'details': 'password=' + password + '\nEncoded: pa%24%24%20word%26extra',
    }))
    response = register(client, password=password)
    assert response.status_code == 502
    assert response.get_json()['rawErrorLog'].endswith('Sign-in failed\npassword=***\nEncoded: ***')
    assert 'extra' not in response.get_data(as_text=True)


def test_registration_success_does_not_include_raw_error_log(client):
    response = register(client)
    assert response.status_code == 201
    assert 'rawErrorLog' not in response.get_json()


@pytest.mark.parametrize('emit_done', [False, True])
def test_worker_exception_preserves_prior_logs_even_after_done(client, monkeypatch, emit_done):
    def failing_task(account_id, events, **kwargs):
        events.put({'type': 'log', 'message': 'Last completed stage'})
        if emit_done:
            events.put(web.GRAPH_OAUTH_DONE)
        raise RuntimeError('Connection failed: password-secret')
    monkeypatch.setattr(web, 'run_graph_oauth_task', failing_task)
    response = register(client)
    assert response.status_code == 502
    assert response.get_json()['rawErrorLog'] == 'Last completed stage\n授权任务异常\nConnection failed: ***'


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
