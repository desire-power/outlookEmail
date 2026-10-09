"""Coordinate external registration requests in the single-worker web app."""

from contextlib import contextmanager
from threading import Lock


_registration_lock = Lock()
_registrations_in_progress = set()


@contextmanager
def claim_external_mail_registration(email):
    """Reject concurrent requests for an address without holding a DB lock."""
    with _registration_lock:
        acquired = email not in _registrations_in_progress
        if acquired:
            _registrations_in_progress.add(email)
    try:
        yield acquired
    finally:
        if acquired:
            with _registration_lock:
                _registrations_in_progress.remove(email)
