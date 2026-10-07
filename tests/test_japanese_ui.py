"""The source overlay must translate UI text without rewriting application data."""
import shutil
import subprocess
from pathlib import Path

import pytest
from flask import Flask, render_template, jsonify
from outlook_web.japanese_ui import register_japanese_ui

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def app():
    app = Flask(__name__, template_folder=str(ROOT / 'templates'), static_folder=str(ROOT / 'static'))
    app.config.update(TESTING=True, SECRET_KEY='japanese-ui-test', UI_LANGUAGE='ja')
    register_japanese_ui(app)
    # Template-only fixtures use the application's unchanged URL names.
    for name in ['favicon', 'index_css', 'login', 'logout', 'index', 'frontend_config']:
        app.add_url_rule('/fixture/' + name, endpoint=name, view_func=lambda: '')
    app.jinja_env.globals.update(frontend_asset_hash='fixture', app_version='1.0',
                                 changelog_url='/changelog', desktop_mode=False)
    return app


def test_login_template_and_inline_script_are_japanese(app):
    with app.test_request_context():
        html = render_template('login.html', csrf_token=lambda: 'fixture-csrf')
    assert 'lang="ja"' in html
    assert 'ログイン - Outlook メール管理' in html
    assert 'ログイン中...' in html
    assert '/assets/localization-ja.js' in html
    assert '登录失败' not in html


def test_template_translation_preserves_interpolated_user_content(app):
    with app.test_request_context():
        html = render_template('email_share.html', account_email='用户邮箱标签@example.com',
                               share_token='用户邮件密码', share_status='active')
    assert 'Outlook 共有メールボックス' in html
    assert 'ユーザー' not in html
    assert '用户邮箱标签@example.com' in html
    assert 'data-share-token="用户邮件密码"' in html
    assert '受信トレイ' in html


def test_all_main_partials_translate_before_rendering(app):
    loader = app.jinja_loader
    for path in (ROOT / 'templates/partials/index').glob('*.html'):
        source, _, _ = loader.get_source(app.jinja_env, 'partials/index/' + path.name)
        assert '导入邮箱账号' not in source
        assert '重新授权并刷新' not in source
        assert '保存设置' not in source
    source, _, _ = loader.get_source(app.jinja_env, 'index.html')
    assert 'Outlook メール管理' in source


def test_javascript_keeps_model_values_and_localizes_display_comparisons(app):
    ui = app.extensions['japanese_ui']
    source = "group.name === '临时邮箱'; proxy === '直连'; msg='加载中...'; btn.textContent === '加载中...';"
    result = ui.javascript(source)
    assert "group.name === '临时邮箱'" in result
    assert "proxy === '直连'" in result
    assert result.count("'読み込み中...'") == 2
    assert ui.javascript("date.toLocaleString('zh-CN')") == "date.toLocaleString('ja-JP')"


@pytest.mark.parametrize('asset', [*sorted(p.relative_to(ROOT / 'static').as_posix()
                         for p in (ROOT / 'static/js/index').glob('*.js')), 'js/email-share.js'])
def test_translated_javascript_is_valid_and_original_file_is_intact(app, asset):
    path = ROOT / 'static' / asset
    original = path.read_bytes()
    response = app.test_client().get('/static/' + asset + '?v=upstream-hash')
    assert response.status_code == 200
    assert response.mimetype == 'application/javascript'
    assert path.read_bytes() == original
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for JavaScript syntax validation')
    subprocess.run([node, '--check', '-'], input=response.data, check=True, capture_output=True)


def test_javascript_revalidates_cache_when_catalog_changes(app):
    client = app.test_client()
    response = client.get('/static/js/index/01-core.js?v=unchanged')
    etag = response.headers['ETag']
    assert 'must-revalidate' in response.headers['Cache-Control']
    assert client.get('/static/js/index/01-core.js?v=unchanged', headers={'If-None-Match': etag}).status_code == 304
    app.extensions['japanese_ui'].catalog['收件箱'] = '受信フォルダ'
    response = client.get('/static/js/index/01-core.js?v=unchanged', headers={'If-None-Match': etag})
    assert response.status_code == 200
    assert response.headers['ETag'] != etag
    assert client.head('/static/js/index/01-core.js').data == b''


def test_catalog_is_idempotent_and_unknown_upstream_strings_fall_back(app):
    ui = app.extensions['japanese_ui']
    for value in ui.catalog.values():
        assert ui.translate(value) == value
    assert ui.translate('新しい上流の文言') == '新しい上流の文言'


def test_json_api_and_unrelated_static_assets_are_unchanged(app):
    @app.get('/api/fixture')
    def api():
        return jsonify(name='默认分组', subject='邮件主题', body='邮箱密码', error='密码错误')
    client = app.test_client()
    assert client.get('/api/fixture').json == {
        'name': '默认分组', 'subject': '邮件主题', 'body': '邮箱密码', 'error': '密码错误',
    }
    css = ROOT / 'static/css/email-share.css'
    assert client.get('/static/css/email-share.css').data == css.read_bytes()
    assert client.get('/static/js/index/missing.js').status_code == 404
    assert client.get('/static/js/index/../../../AGENTS.md').status_code == 404


def test_original_chinese_ui_can_be_selected_without_asset_changes(app):
    app.config['UI_LANGUAGE'] = 'zh-CN'
    with app.test_request_context():
        html = render_template('login.html', csrf_token=lambda: 'fixture-csrf')
    assert 'lang="zh-CN"' in html
    assert '登录 - Outlook 邮件管理' in html
    assert '/assets/localization-ja.js' not in html
    assert app.test_client().get('/static/js/index/01-core.js').data == (ROOT / 'static/js/index/01-core.js').read_bytes()


def test_runtime_asset_has_catalog_and_valid_syntax(app):
    response = app.test_client().get('/assets/localization-ja.js')
    assert response.status_code == 200
    assert 'window.OUTLOOK_JAPANESE_MESSAGES' in response.text
    assert 'パスワードが正しくありません' in response.text
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for JavaScript syntax validation')
    subprocess.run([node, '--check', '-'], input=response.data, check=True, capture_output=True)


def test_runtime_translates_only_display_hooks_and_builtin_group_labels(app):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for runtime verification')
    response = app.test_client().get('/assets/localization-ja.js')
    fixture = r'''
const assert = require('node:assert/strict');
global.window = global;
const groups = [
    {id:1, name:'默认分组', is_system:0},
    {id:2, name:'临时邮箱', is_system:1},
    {id:3, name:'邮箱标签', is_system:0},
    {id:4, name:'临时邮箱', is_system:0},
];
const originalModel = JSON.stringify(groups);
let displayMessage;
global.showToast = message => {displayMessage=message; return 'result';};
global.getGroupOptionLabel = group => group.name;
global.alert = () => {};
global.confirm = () => true;
global.document = {querySelectorAll: () => []};
global.MutationObserver = class {observe() {}};
'''
    assertions = r'''
assert.equal(showToast('网络错误，请重试'), 'result');
assert.equal(displayMessage, '通信エラーが発生しました。再試行してください');
assert.equal(getGroupOptionLabel(groups[0]), '既定グループ');
assert.equal(getGroupOptionLabel(groups[1]), '一時メール');
assert.equal(getGroupOptionLabel(groups[2]), '邮箱标签');
assert.equal(getGroupOptionLabel(groups[3]), '临时邮箱');
assert.equal(JSON.stringify(groups), originalModel);
assert.equal(OutlookJapaneseUI.translate(displayMessage), displayMessage);
'''
    subprocess.run([node, '-'], input=(fixture + response.text + assertions).encode(),
                   check=True, capture_output=True)
