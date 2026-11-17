"""Explicit, bounded loopback observation of one owned live test task.

Only the two hardcoded official HTTPS destinations are reachable. Credentials,
raw request/response bytes and headers are RAM-only. No redirects, retries,
decompression, proxy environment, arbitrary upstream or certificate override.
This module never labels training quality or authenticates for the CLI.
"""
import http.client
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
import ssl
import threading
import time

MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_HEADERS_BYTES = 32768
MAX_HEADERS = 80
ENDPOINTS = {
    'codex': ('chatgpt.com', '/backend-api/codex/responses', '/v1/responses'),
    'claude': ('api.anthropic.com', '/v1/messages', '/v1/messages'),
}
# Only protocol/SDK/auth fields known at this observation point. Unknown headers
# fail locally; they are not copied, reported, or guessed into the catalog.
COMMON_HEADERS = frozenset({'host', 'content-length', 'content-type', 'accept',
    'accept-encoding', 'connection', 'user-agent'})
CODEX_HEADERS = frozenset({'authorization', 'chatgpt-account-id', 'openai-beta',
    'originator', 'version', 'x-codex-installation-id', 'x-codex-routing-hint',
    'x-codex-turn-state', 'x-codex-turn-metadata', 'x-codex-parent-thread-id',
    'x-codex-window-id', 'x-codex-session-id', 'x-openai-internal-codex-residency',
    'x-openai-subagent', 'x-responsesapi-include-timing-metrics', 'session_id',
    'conversation_id', 'x-codex-beta-features', 'x-codex-client-id',
    'x-openai-internal-codex-responses-lite', 'x-oai-attestation', 'traceparent',
    'tracestate', 'session-id', 'thread-id', 'x-client-request-id'})
CLAUDE_HEADERS = frozenset({'authorization', 'x-api-key', 'anthropic-version',
    'anthropic-beta', 'x-app', 'x-stainless-lang', 'x-stainless-package-version',
    'x-stainless-os', 'x-stainless-arch', 'x-stainless-runtime',
    'x-stainless-runtime-version', 'x-stainless-retry-count',
    'x-stainless-timeout', 'x-stainless-helper-method',
    'anthropic-dangerous-direct-browser-access', 'x-client-request-id',
    'x-claude-code-session-id', 'x-claude-code-request-class',
    'x-claude-code-agent-type', 'x-claude-code-prompt-id',
    'x-claude-code-agent-id', 'x-claude-code-parent-agent-id',
    'x-anthropic-additional-protection', 'x-claude-remote-session-origin',
    'x-cowork-enrollment', 'x-client-app', 'x-claude-remote-container-id',
    'x-claude-remote-session-id'})
RESPONSE_HEADERS = frozenset({'content-type', 'request-id', 'x-request-id',
    'x-codex-turn-state', 'x-codex-routing-hint', 'retry-after'})
# Diagnostic recognition is deliberately NOT forwarding authorization. These
# literals were verified in this installed Codex binary; unknown names are only
# counted. Do not populate this table from an incoming request.
DIAGNOSTIC_HEADERS = frozenset({'accept-language', 'baggage', 'content-encoding',
    'transfer-encoding', 'expect', 'x-openai-actor-authorization',
    'openai-agent-assertion', 'x-codex-safety-buffering-enabled',
    'x-codex-safety-buffering-faster-model', 'x-openai-tool-output-truncation-policy',
    'x-openai-encrypted-tool-arguments', 'x-openai-account-routing-override',
    'x-openai-fedramp', 'session-id', 'thread-id', 'x-client-request-id'})
# Fixed public Codex 5cb38e79842a0e0a6dca5bb97874163e8ee50129 source snapshots:
# codex-api/src/requests/headers.rs; codex-api/src/endpoint/responses.rs.
# Recognition never changes policy. These three SDK headers were explicitly
# allowlisted only after the zero-forward real CLI probe matched all three.
DIAGNOSTIC_HEADER_SOURCE_SHA256 = {
    'session-id': '469e1d6dcdc124eb243fe42693365cea81ff633e424c6b2cc8ad169493627dd2',
    'thread-id': '469e1d6dcdc124eb243fe42693365cea81ff633e424c6b2cc8ad169493627dd2',
    'x-client-request-id': 'eb1ba30fc9d428c80dd704bb22130e4d586d72edf2a7589e0e65f6aa9abd7821',
}
UPSTREAM_ERROR_TYPES = frozenset({'invalid_request_error', 'authentication_error',
    'permission_error', 'not_found_error', 'request_too_large', 'rate_limit_error',
    'api_error', 'overloaded_error', 'insufficient_quota', 'invalid_api_key',
    'access_denied', 'rate_limit_exceeded', 'usage_limit_reached'})
ERROR_CODES = frozenset({'request_rejected', 'request_budget_exceeded',
    'bridge_deadline', 'concurrent_request_rejected', 'upstream_unavailable',
    'upstream_redirect_rejected', 'upstream_encoding_rejected',
    'upstream_response_rejected', 'upstream_limit', 'client_write_failed',
    'capture_before_failed', 'capture_after_failed', 'invalid_options',
    'episode_byte_limit', 'request_header_limit', 'request_header_duplicate',
    'request_header_unknown', 'request_header_value_invalid',
    'request_host_mismatch', 'request_content_type_invalid',
    'request_content_length_invalid', 'request_auth_invalid',
    'request_auth_missing', 'request_auth_conflict', 'request_path_invalid',
    'request_body_truncated', 'request_method_invalid', 'probe_only_no_forward'})


class BridgeError(ValueError):
    """Static codes only. Exceptions from clients/upstream never escape."""


def _fail(code):
    raise BridgeError(code)


def header_diagnostics(headers, agent, expected_host):
    """Closed names, booleans and counts only, never values or unknown names."""
    items = list(headers.items());names = [k.lower() for k, _ in items]
    values = {k.lower(): v for k, v in items}
    allowed = COMMON_HEADERS | (CODEX_HEADERS if agent == 'codex' else CLAUDE_HEADERS)
    unexpected = set(names) - allowed
    length = values.get('content-length', '')
    numeric = len(length) <= 12 and length.isascii() and length.isdecimal()
    return {'header_count': len(items), 'duplicate_header_present': len(set(names)) != len(names),
        'authorization_present': 'authorization' in names, 'api_key_present': 'x-api-key' in names,
        'oauth_beta_present': agent == 'claude' and 'oauth-2025-04-20' in {v.strip() for v in values.get('anthropic-beta', '').split(',')},
        'oauth_beta_forwarded_exact': False,
        'host_matches_listener': values.get('host') == expected_host,
        'content_type_supported': values.get('content-type', '').lower() in ('application/json', 'application/json; charset=utf-8'),
        'content_length_present': 'content-length' in names, 'content_length_numeric': numeric,
        'content_length_within_limit': numeric and 0 < int(length) <= MAX_BODY_BYTES,
        'unexpected_header_count': len(unexpected),
        'unexpected_known_header_presence': {k: True for k in sorted(unexpected & DIAGNOSTIC_HEADERS)},
        'unexpected_known_header_source_sha256': {k: DIAGNOSTIC_HEADER_SOURCE_SHA256[k] for k in sorted(unexpected & DIAGNOSTIC_HEADER_SOURCE_SHA256.keys())},
        'unexpected_unknown_header_count': len(unexpected - DIAGNOSTIC_HEADERS)}


def upstream_error_diagnostics(body, content_type, status):
    """Classify exact allowlisted provider fields and fixed message signals.

    Signals are not a conclusion about quota, identity or endpoint policy.
    Provider text, request IDs, unknown type strings and body hashes are omitted.
    """
    if status < 400:
        return None
    result = {'http_status': status, 'json_error_object': False,
              'provider_error_type': 'other_or_unknown', 'message_signals': {}}
    if len(body) > 65536 or content_type.split(';', 1)[0].strip().lower() != 'application/json':
        return result
    def pairs(items):
        value = {}
        for key, child in items:
            if key in value:raise ValueError()
            value[key] = child
        return value
    try:
        root = json.loads(body, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        error = root.get('error') if type(root) is dict else None
        if type(error) is not dict:return result
        result['json_error_object'] = True
        kind = error.get('type')
        if type(kind) is str and kind in UPSTREAM_ERROR_TYPES:
            result['provider_error_type'] = kind
        message = error.get('message')
        if type(message) is str:
            text = message.casefold()
            patterns = {
                'claude_code_credential_scope': ('only authorized for use with claude code', 'cannot be used for other api requests'),
                'authentication_terms': ('authentication', 'invalid api key', 'invalid token', 'unauthorized', 'expired token'),
                'permission_terms': ('permission', 'not allowed', 'forbidden', 'access denied'),
                'quota_terms': ('quota', 'usage limit', 'credit balance', 'rate limit'),
                'region_terms': ('unsupported country', 'not available in your region'),
            }
            result['message_signals'] = {key: any(needle in text for needle in values) for key, values in patterns.items()}
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return result


def _headers(headers, agent, expected_host):
    items = list(headers.items())
    if len(items) > MAX_HEADERS or sum(len(k) + len(v) for k, v in items) > MAX_HEADERS_BYTES:
        _fail('request_header_limit')
    out = {}
    allowed = COMMON_HEADERS | (CODEX_HEADERS if agent == 'codex' else CLAUDE_HEADERS)
    for key, value in items:
        name = key.lower()
        if name in out:
            _fail('request_header_duplicate')
        if name not in allowed:
            _fail('request_header_unknown')
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            _fail('request_header_value_invalid')
        out[name] = value
    if out.get('host') != expected_host:
        _fail('request_host_mismatch')
    if out.get('content-type', '').lower() not in ('application/json', 'application/json; charset=utf-8'):
        _fail('request_content_type_invalid')
    length = out.get('content-length', '')
    if len(length) > 12 or not length.isascii() or not length.isdecimal() or not 0 < int(length) <= MAX_BODY_BYTES:
        _fail('request_content_length_invalid')
    # Never invent credentials, use a dummy real-mode token, or fetch a key.
    auth = out.get('authorization')
    if auth is not None and (not auth.startswith('Bearer ') or not auth[7:].strip()):
        _fail('request_auth_invalid')
    if agent == 'codex':
        if auth is None:
            _fail('request_auth_missing')
    elif auth is None and not out.get('x-api-key'):
        _fail('request_auth_missing')
    elif auth is not None and out.get('x-api-key'):
        # Exactly one CLI-supplied auth mechanism must be present.
        _fail('request_auth_conflict')
    forward = {k: v for k, v in out.items()
               if k not in {'host', 'content-length', 'connection', 'accept-encoding'}}
    forward['accept-encoding'] = 'identity'
    return int(length), forward


def _connect(host, timeout):
    # create_default_context validates platform CA roots and hostname. Do not
    # read proxies, credential files or configurable destinations. The caller
    # should use its reviewed process environment for standard TLS trust roots.
    return http.client.HTTPSConnection(host, 443, timeout=timeout,
                                       context=ssl.create_default_context())


class LiveBridge:
    """Start explicitly; use base_url only for one child CLI process.

    before(request_bytes) -> opaque ticket runs before upstream dispatch.
    after(ticket, response_bytes, content_type, status) runs after exact upstream
    bytes are read and before delivery. Callback exceptions mark capture failed;
    authentic upstream bytes still reach the client. No successful capture claim
    can be made if callback_failures > 0. Callbacks must not persist raw bodies.
    """
    def __init__(self, agent, *, before, after, max_requests=6,
                 total_timeout=120, io_timeout=30, probe_only=False):
        if (agent not in ENDPOINTS or not callable(before) or not callable(after)
                or type(max_requests) is not int or not 1 <= max_requests <= 16
                or type(total_timeout) not in (int, float) or not 1 <= total_timeout <= 300
                or type(io_timeout) not in (int, float) or not 1 <= io_timeout <= 60
                or type(probe_only) is not bool):
            _fail('invalid_options')
        self.agent, self.before, self.after = agent, before, after
        self.max_requests, self.total_timeout, self.io_timeout = max_requests, total_timeout, io_timeout
        self.probe_only = probe_only
        self._nonce = secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        self._metadata_lock = threading.Lock()
        self._stop = threading.Event()
        self._active = set()
        self._started = False
        self._finished = False
        self._stats = {'schema': 'nous.live-bridge/1', 'agent': agent,
            'upstream_requests': 0, 'upstream_responses': 0, 'delivered_responses': 0,
            'request_bytes': 0, 'response_bytes': 0, 'callback_failures': 0,
            'status_counts': {}, 'error_counts': {}, 'raw_body_persisted_by_bridge': False,
            'headers_persisted_by_bridge': False, 'redirects_followed': 0,
            'request_body_reencoded': False, 'response_body_transformed': False,
            'probe_only': probe_only, 'local_requests_seen': 0, 'probe_valid_requests': 0,
            'header_diagnostics': [], 'upstream_error_diagnostics': []}

    def _error(self, code):
        if code not in ERROR_CODES:
            code = 'upstream_unavailable'
        with self._metadata_lock:
            errors = self._stats['error_counts']; errors[code] = errors.get(code, 0) + 1
            if code.startswith('capture_'):
                self._stats['callback_failures'] += 1

    def report(self):
        with self._metadata_lock:
            return {**self._stats, 'status_counts': dict(self._stats['status_counts']),
                    'error_counts': dict(self._stats['error_counts']),
                    'header_diagnostics': [dict(row, unexpected_known_header_presence=dict(row['unexpected_known_header_presence']), unexpected_known_header_source_sha256=dict(row['unexpected_known_header_source_sha256'])) for row in self._stats['header_diagnostics']],
                    'upstream_error_diagnostics': [dict(row, message_signals=dict(row['message_signals'])) for row in self._stats['upstream_error_diagnostics']],
                    'capture_callbacks_passed': self._stats['upstream_responses'] > 0 and self._stats['callback_failures'] == 0,
                    'real_model_response_verified': False}

    def _remaining(self):
        remaining = self._deadline - time.monotonic()
        if self._stop.is_set() or remaining <= 0:
            _fail('bridge_deadline')
        return min(self.io_timeout, remaining)

    def start(self):
        if self._started or self._finished:
            _fail('invalid_options')
        bridge = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            server_version = 'NousObservation'
            sys_version = ''
            def log_message(self, *args):
                pass
            def setup(self):
                super().setup(); self.connection.settimeout(bridge.io_timeout)
            def reply_error(self, status=400):
                # Do not echo path, headers, provider errors or auth values.
                body = b'{"error":{"type":"local_observation_rejected"}}'
                try:
                    self.send_response(status); self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body))); self.send_header('Connection', 'close')
                    self.end_headers(); self.wfile.write(body)
                except (OSError, ValueError):
                    pass
                self.close_connection = True
            def send_error(self, code, message=None, explain=None):
                bridge._error('request_rejected'); self.reply_error(code)
            def reject_method(self):
                bridge._error('request_method_invalid'); self.reply_error(405)
            do_GET = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_CONNECT = do_TRACE = reject_method
            def do_POST(self):
                acquired = False
                try:
                    # Exact URL spelling prevents encoded paths and arbitrary
                    # query routing. Only the SDK's literal beta=true is known.
                    expected = '/' + bridge._nonce + ENDPOINTS[bridge.agent][2]
                    valid_paths = {expected}
                    if bridge.agent == 'claude':
                        valid_paths.add(expected + '?beta=true')
                    if self.path not in valid_paths:
                        _fail('request_path_invalid')
                    with bridge._metadata_lock:
                        if bridge._stats['local_requests_seen'] >= bridge.max_requests:
                            _fail('request_budget_exceeded')
                        bridge._stats['local_requests_seen'] += 1
                        diagnostic = header_diagnostics(self.headers, bridge.agent, f'127.0.0.1:{bridge.port}')
                        bridge._stats['header_diagnostics'].append(diagnostic)
                    length, headers = _headers(self.headers, bridge.agent, f'127.0.0.1:{bridge.port}')
                    beta_exact = diagnostic['oauth_beta_present'] and headers.get('anthropic-beta') == self.headers.get('anthropic-beta')
                    acquired = bridge._lock.acquire(blocking=False)
                    if not acquired:
                        _fail('concurrent_request_rejected')
                    self.connection.settimeout(bridge._remaining())
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        _fail('request_body_truncated')
                    if bridge.probe_only:
                        with bridge._metadata_lock:
                            bridge._stats['probe_valid_requests'] += 1
                        bridge._error('probe_only_no_forward'); self.reply_error(503)
                        return
                    if bridge._stats['upstream_requests'] >= bridge.max_requests:
                        _fail('request_budget_exceeded')
                    body, content_type, status, response_headers = bridge._exchange(raw, headers, beta=self.path.endswith('?beta=true'), diagnostic=diagnostic, beta_exact=beta_exact)
                    self.send_response(status)
                    for key, value in response_headers.items():
                        self.send_header(key, value)
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header('Connection', 'close'); self.end_headers()
                    try:
                        self.wfile.write(body); self.wfile.flush()
                    except (OSError, ValueError):
                        _fail('client_write_failed')
                    with bridge._metadata_lock:
                        bridge._stats['delivered_responses'] += 1
                    self.close_connection = True
                except BridgeError as error:
                    bridge._error(str(error)); self.reply_error(502)
                except Exception:
                    bridge._error('request_rejected'); self.reply_error(502)
                finally:
                    if acquired:
                        bridge._lock.release()
        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._server.daemon_threads = True
        self._server.handle_error = lambda *args: None
        self.port = self._server.server_address[1]
        self.base_url = f'http://127.0.0.1:{self.port}/' + self._nonce
        self._deadline = time.monotonic() + self.total_timeout
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self._started = True; self._thread.start()
        return self

    def _exchange(self, raw, headers, *, beta=False, diagnostic=None, beta_exact=False):
        self._remaining()
        if self._stats['request_bytes'] + self._stats['response_bytes'] + len(raw) > MAX_TOTAL_BYTES:
            _fail('episode_byte_limit')
        ticket = None; captured = True
        try:
            ticket = self.before(raw)
        except Exception:
            captured = False; self._error('capture_before_failed')
        host, path, _ = ENDPOINTS[self.agent]
        if beta and self.agent == 'claude':
            path += '?beta=true'
        try:
            conn = _connect(host, self._remaining())
        except BridgeError:
            raise
        except Exception:
            _fail('upstream_unavailable')
        with self._metadata_lock:
            self._active.add(conn)
        try:
            with self._metadata_lock:
                self._stats['upstream_requests'] += 1
                self._stats['request_bytes'] += len(raw)
            conn.request('POST', path, body=raw, headers=headers)
            if diagnostic is not None:
                with self._metadata_lock:
                    diagnostic['oauth_beta_forwarded_exact'] = beta_exact
            response = conn.getresponse()
            status = response.status
            if not 100 <= status <= 599:
                _fail('upstream_response_rejected')
            if 300 <= status < 400:
                _fail('upstream_redirect_rejected')
            pairs = response.getheaders(); seen = set(); safe_headers = {}
            if len(pairs) > MAX_HEADERS or sum(len(k) + len(v) for k, v in pairs) > MAX_HEADERS_BYTES:
                _fail('upstream_response_rejected')
            for name, value in pairs:
                name = name.lower()
                if any(ord(c) < 32 or ord(c) == 127 for c in value):
                    _fail('upstream_response_rejected')
                if name in {'content-type', 'content-length', 'content-encoding'} and name in seen:
                    _fail('upstream_response_rejected')
                seen.add(name)
                if name in RESPONSE_HEADERS:
                    safe_headers[name] = value
            encoding = response.getheader('content-encoding', 'identity').lower()
            if encoding != 'identity':
                _fail('upstream_encoding_rejected')
            content_type = safe_headers.get('content-type', '')
            if content_type.split(';', 1)[0].strip().lower() not in {'application/json', 'text/event-stream'}:
                _fail('upstream_response_rejected')
            length = response.getheader('content-length')
            if length is not None and (not length.isascii() or not length.isdecimal() or int(length) > MAX_BODY_BYTES):
                _fail('upstream_limit')
            body = bytearray()
            while True:
                timeout = self._remaining()
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)
                chunk = response.read(min(65536, MAX_BODY_BYTES + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    _fail('upstream_limit')
                if self._stats['request_bytes'] + self._stats['response_bytes'] + len(body) > MAX_TOTAL_BYTES:
                    _fail('episode_byte_limit')
            if length is not None and len(body) != int(length):
                _fail('upstream_response_rejected')
            body = bytes(body)
            with self._metadata_lock:
                self._stats['upstream_responses'] += 1
                self._stats['response_bytes'] += len(body)
                key = str(status); counts = self._stats['status_counts']; counts[key] = counts.get(key, 0) + 1
                diagnostic = upstream_error_diagnostics(body, content_type, status)
                if diagnostic is not None:
                    self._stats['upstream_error_diagnostics'].append(diagnostic)
            if captured:
                try:
                    self.after(ticket, body, content_type, status)
                except Exception:
                    self._error('capture_after_failed')
            return body, content_type, status, safe_headers
        except BridgeError:
            raise
        except Exception:
            _fail('upstream_unavailable')
        finally:
            conn.close()
            with self._metadata_lock:
                self._active.discard(conn)

    def stop(self):
        if self._finished:
            return
        self._stop.set()
        if self._started:
            self._server.shutdown(); self._server.server_close()
            with self._metadata_lock:
                active = list(self._active)
            for conn in active:
                conn.close()
            self._thread.join(timeout=1)
        self._finished = True

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()
