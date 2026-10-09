import pytest

from outlook_web.graph_oauth_diagnostics import extract_graph_login_error


@pytest.mark.parametrize(('page', 'expected'), [
    ('var sErrTxt = "If you don\'t remember your password, reset it.";', "If you don't remember your password, reset it."),
    (r'''{"sErrTxt":"Account \"locked\". Check \u0027security\u0027.\nTry again."}''', 'Account "locked". Check \'security\'.\nTry again.'),
    (r"sErrTxt:'If you don\'t remember it, use \x22reset\x22.'", 'If you don\'t remember it, use "reset".'),
    ('sErrTxt = "First &amp; second&#39;s error";', "First & second's error"),
    ('<div id="error">Incorrect password</div>', None),
])
def test_extracts_complete_javascript_error(page, expected):
    assert extract_graph_login_error(page) == expected
