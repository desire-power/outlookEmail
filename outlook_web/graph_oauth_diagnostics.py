"""Preserve OAuth diagnostic messages for external registration failures."""

import html
import json
import re
from urllib.parse import quote, quote_plus


def extract_graph_login_error(page):
    """Read sErrTxt without stopping at apostrophes or escaped quotes."""
    match = re.search(
        r'''\bsErrTxt["']?\s*[:=]\s*(?P<quote>["'])(?P<text>(?:\\.|(?!(?P=quote))[^\\])*)(?P=quote)''',
        page, re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    value = match.group('text')
    if match.group('quote') == '"':
        try:
            value = json.loads('"' + value + '"')
            return html.unescape(value)
        except ValueError:
            pass
    escapes = {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f'}

    def decode_escape(escaped):
        sequence = escaped.group()[1:]
        if sequence.startswith(('u', 'x')) and len(sequence) > 1:
            return chr(int(sequence[1:], 16))
        return escapes.get(sequence, sequence)

    return html.unescape(re.sub(r'\\(?:u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)', decode_escape, value))


def mask_graph_oauth_secrets(message, secrets):
    values = {
        variant for secret in secrets if isinstance(secret, str) and secret
        for variant in (secret, quote(secret, safe=''), quote_plus(secret, safe=''))
    }
    for value in sorted(values, key=len, reverse=True):
        message = message.replace(value, '***')
    return message


def append_graph_oauth_event_log(lines, event):
    if event.get('type') not in {'start', 'log', 'error'}:
        return
    for field in ('message', 'details'):
        if event.get(field):
            lines.append(str(event[field]))
