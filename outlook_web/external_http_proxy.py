"""Validation for the optional HTTP proxy supplied to external account APIs."""

from urllib.parse import urlparse


def normalize_external_http_proxy(value):
    if not isinstance(value, str):
        raise ValueError('httpProxy must be a string')
    value = value.strip()
    if not value:
        return ''
    error = 'httpProxy must be an HTTP proxy URL with a host and port'
    try:
        parsed = urlparse(value)
        valid = (
            parsed.scheme.lower() == 'http'
            and parsed.hostname
            and parsed.port is not None
            and 1 <= parsed.port <= 65535
            and parsed.path in {'', '/'}
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
        )
    except ValueError:
        raise ValueError(error) from None
    if not valid:
        raise ValueError(error)
    return value
