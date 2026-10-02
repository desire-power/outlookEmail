"""Additive account APIs using the existing external API Key authentication."""


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

    account = get_account_by_email(email_addr)
    if not account:
        return jsonify({'success': False, 'error': 'Email account not found'}), 404
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
    allowed_fields = {'email', 'password', 'group_id', 'remark'}
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
        )
        if outcome['status'] != 'added':
            return jsonify({'success': False, 'error': 'Unable to register email account'}), 409
        db.commit()
    finally:
        if db.in_transaction:
            db.rollback()
    return jsonify({
        'success': True,
        'account': {
            'id': outcome['id'], 'email': email_addr, 'group_id': group_id,
            'account_type': 'outlook', 'is_authorized': False,
        },
    }), 201
