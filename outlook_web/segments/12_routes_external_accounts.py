"""Additive account APIs using the existing external API Key authentication."""

from outlook_web.external_http_proxy import normalize_external_http_proxy
from outlook_web.graph_oauth_diagnostics import append_graph_oauth_event_log


@app.after_request
def prevent_external_account_api_caching(response):
    if request.endpoint in {'api_external_latest_emails', 'api_external_register_account'}:
        response.headers['Cache-Control'] = 'no-store'
    return response


def is_external_account_api_email(value):
    return (
        isinstance(value, str) and len(value) <= 254
        and re.fullmatch(r'[^@\s]+@[^@\s]+', value) is not None
    )


class ExternalMailRegistrationConflict(Exception):
    """An address was registered by another request while OAuth was running."""


def save_external_mail_authorization(upload_row, client_id, refresh_token, authorization_type=None):
    """Insert a new account only; check ownership and save under one write lock."""
    db = get_db()
    email_addr = normalize_email_address(upload_row['email'])
    try:
        db.execute('BEGIN IMMEDIATE')
        if email_exists_as_primary(email_addr) or email_exists_as_alias(email_addr):
            raise ExternalMailRegistrationConflict('Email account already exists')
        cursor = db.execute(ACCOUNT_INSERT_SQL, build_account_insert_values(
            email_addr,
            get_upload_account_plain_password(upload_row),
            client_id,
            refresh_token,
            group_id=resolve_upload_group_id(upload_row['group_id']),
            remark=upload_row['remark'] or '',
            proxy_url=upload_row['proxy_url'] or '',
            imap_host=IMAP_SERVER_NEW,
            imap_port=IMAP_PORT,
        ))
        if cursor.rowcount != 1:
            raise ExternalMailRegistrationConflict('Email account already exists')
        account_id = int(cursor.lastrowid)
        apply_account_tag_ids(account_id, decode_upload_tag_ids(upload_row['tag_ids']), db)
        db.execute(
            '''
            UPDATE accounts
            SET authorization_type = ?, refresh_token_updated_at = CURRENT_TIMESTAMP,
                last_refresh_status = 'never', last_refresh_error = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            ''',
            (normalize_outlook_authorization_type(authorization_type, strict=True), account_id),
        )
        mark_upload_account_authorized(int(upload_row['id']))
        db.commit()
        return {'account_id': account_id, 'created': True}
    except Exception:
        db.rollback()
        raise


def authorize_external_mail_registration(upload_account_id, error_log=None):
    events = queue.Queue()
    conflict = False
    task_exception = None

    def save_new_account(*args, **kwargs):
        nonlocal conflict
        try:
            return save_external_mail_authorization(*args, **kwargs)
        except ExternalMailRegistrationConflict:
            conflict = True
            raise

    try:
        # Reuse extraction and validation, but never upsert an existing formal account.
        run_graph_oauth_task(upload_account_id, events, mode='graph', save_authorization=save_new_account)
    except Exception as exc:
        if conflict:
            raise ExternalMailRegistrationConflict('Email account already exists')
        task_exception = exc
    if conflict:
        raise ExternalMailRegistrationConflict('Email account already exists')
    authorized = None
    completed_successfully = False
    while not events.empty():
        event = events.get_nowait()
        if event is GRAPH_OAUTH_DONE:
            break
        if not isinstance(event, dict):
            continue
        if error_log is not None:
            append_graph_oauth_event_log(error_log, event)
        if event.get('type') == 'success' and event.get('success'):
            authorized = event
        elif event.get('type') == 'complete':
            completed_successfully = bool(event.get('success'))
    if task_exception is not None:
        if error_log is not None:
            append_graph_oauth_event_log(error_log, {
                'type': 'error', 'message': '授权任务异常', 'details': str(task_exception),
            })
        return None
    return authorized if completed_successfully else None


@app.route('/api/external/latest-emails', methods=['GET'])
@csrf_exempt
@api_key_required
def api_external_latest_emails():
    email_addr = normalize_email_address(get_query_arg_preserve_plus('email', ''))
    if not is_external_account_api_email(email_addr):
        return jsonify({'success': False, 'error': 'A valid email is required'}), 400
    folder = request.args.get('folder', 'inbox').strip().lower()
    if folder not in {'inbox', 'junkemail'}:
        return jsonify({'success': False, 'error': 'folder must be inbox or junkemail'}), 400
    try:
        top = int(request.args.get('top', '1'))
    except (TypeError, ValueError):
        top = 0
    if not 1 <= top <= 50:
        return jsonify({'success': False, 'error': 'top must be an integer from 1 to 50'}), 400
    try:
        http_proxy = normalize_external_http_proxy(request.args.get('httpProxy', ''))
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400

    account = get_account_by_email(email_addr)
    if not account:
        return jsonify({'success': False, 'error': 'Email account not found'}), 404
    if http_proxy:
        # Override only this fetch, including inherited fallback proxies.
        account = dict(account, proxy_url=http_proxy, fallback_proxy_url_1='', fallback_proxy_url_2='')
    try:
        # Always contact the mail server, regardless of local retention settings.
        result = fetch_account_emails(account, folder, 0, top)
    except Exception:
        return jsonify({'success': False, 'error': 'Unable to fetch latest emails'}), 502
    if not result.get('success'):
        # Do not expose upstream credential-bearing errors to callers.
        return jsonify({'success': False, 'error': 'Unable to fetch latest emails'}), 502
    return jsonify({
        'success': True,
        'emails': result.get('emails', []),
        'method': result.get('method', ''),
        'has_more': bool(result.get('has_more')),
        'requested_email': email_addr,
        'resolved_email': account['email'],
    })


@app.route('/api/external/mail', methods=['POST'])
@csrf_exempt
@api_key_required
def api_external_register_account():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': 'A JSON object is required'}), 400
    allowed_fields = {'email', 'password', 'group_id', 'remark', 'httpProxy'}
    if set(data) - allowed_fields:
        return jsonify({'success': False, 'error': 'Unsupported registration field'}), 400
    for field in ('email', 'password', 'remark'):
        if field in data and not isinstance(data[field], str):
            return jsonify({'success': False, 'error': f'{field} must be a string'}), 400
    email_addr = normalize_email_address(data.get('email', ''))
    if not is_external_account_api_email(email_addr):
        return jsonify({'success': False, 'error': 'A valid email is required'}), 400
    password = data.get('password', '')
    if not password.strip():
        return jsonify({'success': False, 'error': 'password is required'}), 400
    group_id = data.get('group_id', DEFAULT_GROUP_ID)
    if type(group_id) is not int or group_id <= 0:
        return jsonify({'success': False, 'error': 'group_id must be a positive integer'}), 400
    try:
        http_proxy = normalize_external_http_proxy(data.get('httpProxy', ''))
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400

    db = get_db()
    try:
        # Serialize duplicate checks because the existing UNIQUE email is case-sensitive.
        db.execute('BEGIN IMMEDIATE')
        if not get_group_by_id(group_id):
            return jsonify({'success': False, 'error': 'Group not found'}), 400
        existing_upload = db.execute(
            'SELECT id FROM outlook_upload_accounts WHERE LOWER(email) = ? LIMIT 1', (email_addr,),
        ).fetchone()
        if existing_upload or email_exists_as_primary(email_addr) or email_exists_as_alias(email_addr):
            return jsonify({'success': False, 'error': 'Email account already exists'}), 409
        outcome = add_upload_account(
            email_addr, password,
            group_id=group_id,
            remark=sanitize_input(data.get('remark', '').strip(), max_length=500),
            proxy_url=http_proxy,
        )
        if outcome['status'] != 'added':
            return jsonify({'success': False, 'error': 'Unable to register email account'}), 409
        db.commit()
    finally:
        if db.in_transaction:
            db.rollback()
    # Release the SQLite write lock before making external OAuth requests.
    error_log = []
    try:
        authorization = authorize_external_mail_registration(outcome['id'], error_log)
    except ExternalMailRegistrationConflict:
        return jsonify({'success': False, 'error': 'Email account already exists'}), 409
    upload_row = get_upload_account_for_graph_auth(outcome['id'])
    account_payload = {
        'id': outcome['id'], 'email': email_addr, 'group_id': group_id,
        'account_type': 'outlook', 'is_authorized': bool(upload_row and upload_row['is_authorized']),
    }
    if not authorization or not account_payload['is_authorized']:
        return jsonify({
            'success': False,
            'error': 'OAuth authorization failed',
            'rawErrorLog': graph_oauth_safe_details('\n'.join(error_log), secrets=[password]),
            'account': account_payload,
        }), 502
    account_payload['account_id'] = authorization['account_id']
    account_payload['authorization_type'] = authorization['authorization_type']
    return jsonify({'success': True, 'account': account_payload}), 201
