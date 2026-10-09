"""Exercise HTTP CONNECT over real loopback sockets, without external services."""

import base64
import ipaddress
import os
import socketserver
import ssl
import tempfile
import threading
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

os.environ.setdefault('SECRET_KEY', 'test-secret-key')
if 'DATABASE_PATH' not in os.environ:
    os.environ['DATABASE_PATH'] = os.path.join(
        tempfile.mkdtemp(prefix='outlookEmail-proxy-transport-tests-'), 'test.db',
    )

import web_outlook_app as web


@pytest.fixture
def connect_proxy(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'mail.example.test')])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([
            x509.DNSName('mail.example.test'), x509.IPAddress(ipaddress.ip_address('127.0.0.1')),
        ]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(cert_path, key_path)
    state = {'requests': [], 'allow_connect': True, 'cert_path': str(cert_path)}

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(3)
            connect_line = self.rfile.readline().decode().strip()
            headers = {}
            while line := self.rfile.readline().decode().strip():
                name, value = line.split(':', 1)
                headers[name.lower()] = value.strip()
            state['requests'].append((connect_line, headers))
            if not state['allow_connect']:
                self.wfile.write(b'HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n')
                return
            self.wfile.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
            with tls_context.wrap_socket(self.connection, server_side=True) as tunnel:
                if ':443 ' in connect_line:
                    with tunnel.makefile('rb') as stream:
                        while stream.readline().strip():
                            pass
                    tunnel.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK')
                else:
                    tunnel.sendall(b'* OK Local IMAP test\r\n')
                    with tunnel.makefile('rb') as stream:
                        while line := stream.readline():
                            tag, command = line.split()[:2]
                            if command == b'CAPABILITY':
                                tunnel.sendall(b'* CAPABILITY IMAP4rev1\r\n' + tag + b' OK done\r\n')
                            elif command == b'LOGOUT':
                                tunnel.sendall(b'* BYE\r\n' + tag + b' OK done\r\n')
                                break

    with socketserver.TCPServer(('127.0.0.1', 0), Handler) as server:
        state['url'] = f'http://127.0.0.1:{server.server_address[1]}'
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        try:
            yield state
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize('transport', ['https', 'imap'])
@pytest.mark.parametrize('allow_connect', [True, False])
def test_http_proxy_connect_tls_and_authentication(connect_proxy, monkeypatch, transport, allow_connect):
    state = connect_proxy
    state['allow_connect'] = allow_connect
    proxy = state['url'].replace('http://', 'http://test-user:test%2Bpassword@')
    if transport == 'https':
        def connect():
            return web.request_with_proxy_failover(
                'GET', 'https://mail.example.test/check', proxy_url=proxy,
                verify=state['cert_path'], timeout=3,
            )
        expected_error = web.requests.exceptions.ProxyError
    else:
        original_imap_ssl = web.imaplib.IMAP4_SSL
        # Trust only this test certificate; retain the real TLS and IMAP implementation.
        monkeypatch.setattr(web.imaplib, 'IMAP4_SSL', lambda *args, **kwargs: original_imap_ssl(
            *args, **kwargs, ssl_context=ssl.create_default_context(cafile=state['cert_path']),
        ))
        def connect():
            return web.create_imap_connection('127.0.0.1', 993, proxy)
        expected_error = web.socks.ProxyError
    if allow_connect:
        connection = connect()
        if transport == 'https':
            assert connection.status_code == 200
            assert connection.text == 'OK'
            connection.close()
        else:
            assert 'IMAP4REV1' in connection.capabilities
            connection.logout()
    else:
        with pytest.raises(expected_error):
            connect()
    assert len(state['requests']) == 1
    connect_line, headers = state['requests'][0]
    target = 'mail.example.test:443' if transport == 'https' else '127.0.0.1:993'
    assert connect_line.split()[:2] == ['CONNECT', target]
    auth_scheme, auth_value = headers['proxy-authorization'].split()
    assert auth_scheme.lower() == 'basic'
    assert base64.b64decode(auth_value) == b'test-user:test+password'
