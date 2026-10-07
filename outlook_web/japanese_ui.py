"""Japanese presentation overlay without changing upstream templates or assets."""

import hashlib
import json
import os
import re
from pathlib import Path

from flask import Response, request
from jinja2 import BaseLoader


TEMPLATES = {'index.html', 'login.html', 'email_share.html'}
# These literals also identify persisted data or accepted configuration values.
MODEL_LITERALS = re.compile(r"(['\"])(临时邮箱|默认分组|直连)\1")


class JapaneseUI:
    def __init__(self, app):
        self.app = app
        self.static_root = Path(app.static_folder)
        self.catalog = json.loads((self.static_root / 'locales/ja.json').read_text(encoding='utf-8'))
        self.pattern = re.compile('|'.join(re.escape(key) for key in sorted(self.catalog, key=len, reverse=True)))

    def enabled(self):
        return self.app.config['UI_LANGUAGE'] == 'ja'

    def translate(self, source):
        # One pass: translated Japanese must not be translated again as Chinese.
        return self.pattern.sub(lambda match: self.catalog[match.group()], source)

    def javascript(self, source):
        protected = []

        def preserve(match):
            protected.append(match.group())
            return f'__OUTLOOK_JA_MODEL_{len(protected) - 1}__'

        source = self.translate(MODEL_LITERALS.sub(preserve, source))
        for index, literal in enumerate(protected):
            source = source.replace(f'__OUTLOOK_JA_MODEL_{index}__', literal)
        return source.replace("'zh-CN'", "'ja-JP'").replace('"zh-CN"', '"ja-JP"')


class JapaneseTemplateLoader(BaseLoader):
    def __init__(self, original, ui):
        self.original = original
        self.ui = ui

    def get_source(self, environment, template):
        source, filename, uptodate = self.original.get_source(environment, template)
        if self.ui.enabled() and (template in TEMPLATES or template.startswith('partials/index/')):
            # Jinja expressions are evaluated afterwards; account names and other data stay intact.
            source = self.ui.javascript(source) if template == 'login.html' else self.ui.translate(source)
            source = source.replace('lang="zh-CN"', 'lang="ja"').replace('lang="ja-JP"', 'lang="ja"')
            if template in TEMPLATES:
                source = source.replace('</head>',
                    '<script defer src="{{ url_for(\'japanese_ui_runtime\') }}"></script>\n</head>', 1)
        return source, filename, uptodate

    def list_templates(self):
        return self.original.list_templates()


def register_japanese_ui(app):
    app.config.setdefault('UI_LANGUAGE', os.getenv('OUTLOOK_UI_LANGUAGE', 'ja'))
    ui = JapaneseUI(app)
    app.extensions['japanese_ui'] = ui
    app.jinja_loader = JapaneseTemplateLoader(app.jinja_loader, ui)

    def javascript_response(source):
        response = Response(source, mimetype='application/javascript')
        response.set_etag(hashlib.sha256(source.encode('utf-8')).hexdigest())
        # Revalidate even versioned upstream URLs: the catalog may change independently.
        response.headers['Cache-Control'] = 'no-cache, max-age=0, must-revalidate'
        return response.make_conditional(request)

    @app.before_request
    def localize_japanese_javascript():
        if not ui.enabled() or request.endpoint != 'static' or request.method not in {'GET', 'HEAD'}:
            return None
        filename = (request.view_args or {}).get('filename', '')
        if not (filename.startswith('js/index/') or filename == 'js/email-share.js') or not filename.endswith('.js'):
            return None
        path = (ui.static_root / filename).resolve()
        if not path.is_relative_to(ui.static_root.resolve()) or not path.is_file():
            return None
        return javascript_response(ui.javascript(path.read_text(encoding='utf-8')))

    @app.route('/assets/localization-ja.js', endpoint='japanese_ui_runtime')
    def japanese_ui_runtime():
        source = (ui.static_root / 'js/localization-ja.js').read_text(encoding='utf-8')
        catalog = json.dumps(ui.catalog, ensure_ascii=False)
        return javascript_response('window.OUTLOOK_JAPANESE_MESSAGES = ' + catalog + ';\n' + source)
