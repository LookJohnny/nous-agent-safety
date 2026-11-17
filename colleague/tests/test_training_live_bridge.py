"""All upstream I/O is mocked; local listener tests never call a model."""
import http.client
import io
import json
from pathlib import Path
import ssl
import sys
import time
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hooks'))
import nous_training_live_bridge as bridge


class Response:
    def __init__(self, body=b'data: {"type":"test"}\n\n', status=200, headers=None):
        self.status = status
        self.headers = headers if headers is not None else [('Content-Type', 'text/event-stream'), ('Content-Length', str(len(body)))]
        self.data = io.BytesIO(body)
    def getheaders(self): return self.headers
    def getheader(self, key, default=None):
        values=[v for k,v in self.headers if k.lower()==key.lower()]
        return ', '.join(values) if values else default
    def read(self, size): return self.data.read(size)


class Connection:
    sock = None
    def __init__(self, response=None): self.response=response or Response(); self.calls=[]; self.closed=False
    def request(self, *args, **kwargs): self.calls.append((args,kwargs))
    def getresponse(self): return self.response
    def close(self): self.closed=True


class LiveBridgeTests(unittest.TestCase):
    def request(self, live, *, path=None, method='POST', body=b'{"messages":[]}', headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',live.port,timeout=2)
        h={'Content-Type':'application/json','Authorization':'Bearer test-only-not-a-credential'}
        if headers is not None:h=headers
        path=path or '/' + live._nonce + bridge.ENDPOINTS[live.agent][2]
        try:
            conn.request(method,path,body=body,headers=h)
            response=conn.getresponse();return response.status,response.read(),response.getheaders()
        finally:conn.close()
    def live(self, agent='codex', **kwargs):
        return bridge.LiveBridge(agent,before=kwargs.pop('before',lambda raw:raw),after=kwargs.pop('after',lambda *x:None),**kwargs)
    def test_exact_bytes_and_fixed_codex_destination(self):
        captured=[];conn=Connection();original=b'{ "input" : [] }'
        with patch.object(bridge,'_connect',return_value=conn) as create, self.live(before=lambda b:captured.append(b) or 'ticket',after=lambda *a:captured.append(a)) as live:
            status,body,_=self.request(live,body=original)
            self.assertEqual(status,200);self.assertEqual(body,b'data: {"type":"test"}\n\n')
            self.assertEqual(captured[0],original);self.assertEqual(captured[1],('ticket',body,'text/event-stream',200))
            self.assertEqual(create.call_args.args[0],'chatgpt.com')
            args,kw=conn.calls[0];self.assertEqual(args,('POST','/backend-api/codex/responses'));self.assertEqual(kw['body'],original)
            self.assertEqual(kw['headers']['authorization'],'Bearer test-only-not-a-credential')
            self.assertEqual(kw['headers']['accept-encoding'],'identity')
            self.assertTrue(live.report()['capture_callbacks_passed'])
            self.assertNotIn('test-only-not-a-credential',json.dumps(live.report()))
        self.assertTrue(conn.closed)
    def test_claude_key_and_literal_beta_query_preserved(self):
        conn=Connection()
        with patch.object(bridge,'_connect',return_value=conn) as create,self.live('claude') as live:
            path='/'+live._nonce+'/v1/messages?beta=true'
            self.assertEqual(self.request(live,path=path,headers={'Content-Type':'application/json','x-api-key':'test-only-key'})[0],200)
            self.assertEqual(create.call_args.args[0],'api.anthropic.com')
            self.assertEqual(conn.calls[0][0],('POST','/v1/messages?beta=true'))
    def test_claude_oauth_header_allowed_without_inventing_auth(self):
        with patch.object(bridge,'_connect',return_value=Connection()),self.live('claude') as live:
            self.assertEqual(self.request(live)[0],200)
    def test_claude_oauth_beta_presence_and_exact_forwarding_are_booleans(self):
        h={'Content-Type':'application/json','Authorization':'Bearer synthetic-only','anthropic-beta':'private-mode, oauth-2025-04-20'}
        conn=Connection()
        with patch.object(bridge,'_connect',return_value=conn),self.live('claude') as live:
            self.assertEqual(self.request(live,headers=h)[0],200)
            d=live.report()['header_diagnostics'][0]
            self.assertTrue(d['oauth_beta_present']);self.assertTrue(d['oauth_beta_forwarded_exact'])
            self.assertEqual(conn.calls[0][1]['headers']['anthropic-beta'],h['anthropic-beta'])
            self.assertNotIn('private-mode',json.dumps(live.report()))
        with patch.object(bridge,'_connect') as create,self.live('claude',probe_only=True) as live:
            self.assertEqual(self.request(live,headers=h)[0],503)
            d=live.report()['header_diagnostics'][0]
            self.assertTrue(d['oauth_beta_present']);self.assertFalse(d['oauth_beta_forwarded_exact']);create.assert_not_called()
    def test_unknown_paths_nonce_queries_and_methods_fail_before_upstream(self):
        with patch.object(bridge,'_connect') as create,self.live() as live:
            for path,method in [('/bad/v1/responses','POST'),('/'+live._nonce+'/v1/responses?x=1','POST'),('/'+live._nonce+'/v1/responses','GET'),('https://example.invalid/v1/responses','POST')]:
                self.assertGreaterEqual(self.request(live,path=path,method=method)[0],400)
            create.assert_not_called()
    def test_credentials_required_and_unknown_headers_rejected(self):
        with patch.object(bridge,'_connect') as create,self.live() as live:
            for extra in [{},{'Authorization':'Basic forbidden'},{'Authorization':'Bearer x','Cookie':'not-forwarded'},{'Authorization':'Bearer x','Content-Encoding':'gzip'},{'Authorization':'Bearer x','Host':'elsewhere'}]:
                self.assertGreaterEqual(self.request(live,headers={'Content-Type':'application/json',**extra})[0],400)
            create.assert_not_called()
    def test_claude_dual_credentials_rejected(self):
        with patch.object(bridge,'_connect') as create,self.live('claude') as live:
            self.assertGreaterEqual(self.request(live,headers={'Content-Type':'application/json','Authorization':'Bearer a','x-api-key':'b'})[0],400)
            create.assert_not_called()
    def test_request_budget(self):
        with patch.object(bridge,'_connect',side_effect=lambda *a:Connection()) as create,self.live(max_requests=1) as live:
            self.assertEqual(self.request(live)[0],200);self.assertEqual(self.request(live)[0],502)
            self.assertEqual(create.call_count,1);self.assertIn('request_budget_exceeded',live.report()['error_counts'])
    def test_redirect_not_followed_or_copied(self):
        response=Response(b'private-provider-body',302,[('Content-Type','application/json'),('Location','https://elsewhere.invalid')])
        with patch.object(bridge,'_connect',return_value=Connection(response)) as create,self.live() as live:
            status,body,headers=self.request(live)
            self.assertEqual(status,502);self.assertNotIn(b'private-provider-body',body)
            self.assertNotIn('location',{k.lower() for k,v in headers});self.assertEqual(create.call_count,1)
    def test_upstream_compression_rejected(self):
        response=Response(b'compressed',[200][0],[('Content-Type','text/event-stream'),('Content-Encoding','gzip')])
        with patch.object(bridge,'_connect',return_value=Connection(response)),self.live() as live:
            self.assertEqual(self.request(live)[0],502);self.assertIn('upstream_encoding_rejected',live.report()['error_counts'])
    def test_response_limit_and_episode_limit(self):
        with patch.object(bridge,'MAX_BODY_BYTES',24),patch.object(bridge,'_connect',side_effect=lambda *a:Connection(Response(b'x'*25))),self.live() as live:
            self.assertEqual(self.request(live)[0],502);self.assertIn('upstream_limit',live.report()['error_counts'])
        with patch.object(bridge,'MAX_TOTAL_BYTES',14),patch.object(bridge,'_connect') as create,self.live() as live:
            self.assertEqual(self.request(live,body=b'{' + b'x'*20 + b'}')[0],502);create.assert_not_called()
    def test_callback_failures_never_rewrite_authentic_response_or_claim_capture(self):
        def fail(*args):raise RuntimeError('source-secret-must-not-be-reported')
        for options in ({'before':fail},{'after':fail}):
            with patch.object(bridge,'_connect',return_value=Connection()),self.live(**options) as live:
                status,body,_=self.request(live);self.assertEqual(status,200);self.assertIn(b'data:',body)
                report=live.report();self.assertFalse(report['capture_callbacks_passed']);self.assertEqual(report['callback_failures'],1)
                self.assertNotIn('source-secret',json.dumps(report))
    def test_upstream_errors_static_and_no_retry(self):
        with patch.object(bridge,'_connect',side_effect=RuntimeError('do-not-log-secret')) as create,self.live() as live:
            status,body,_=self.request(live);self.assertEqual(status,502);self.assertNotIn(b'do-not-log',body);self.assertEqual(create.call_count,1)
    def test_deadline_and_stop(self):
        with patch.object(bridge,'_connect') as create,self.live() as live:
            live._deadline=time.monotonic()-1;self.assertEqual(self.request(live)[0],502);create.assert_not_called()
        live.stop();self.assertRaises(bridge.BridgeError,live.start)
    def test_tls_verification_fixed_host_port_and_no_proxy(self):
        with patch.object(http.client,'HTTPSConnection') as conn:
            bridge._connect('chatgpt.com',5)
            args,kw=conn.call_args;self.assertEqual(args,('chatgpt.com',443))
            self.assertTrue(kw['context'].check_hostname);self.assertEqual(kw['context'].verify_mode,ssl.CERT_REQUIRED)
    def test_invalid_options_no_endpoint_override_api(self):
        for opts in ({'max_requests':0},{'max_requests':17},{'total_timeout':301},{'io_timeout':61}):
            self.assertRaises(bridge.BridgeError,self.live,**opts)
        self.assertRaises(bridge.BridgeError,self.live,agent='unknown')
        self.assertRaises(TypeError,self.live,upstream='http://elsewhere.invalid')
    def test_duplicate_ambiguous_headers_rejected(self):
        from email.message import Message
        h=Message()
        for k,v in [('Host','127.0.0.1:1234'),('Content-Type','application/json'),('Content-Length','2'),('content-length','2'),('Authorization','Bearer test')]:h[k]=v
        with self.assertRaises(bridge.BridgeError):bridge._headers(h,'codex','127.0.0.1:1234')
    def test_concurrent_request_denied_and_inbound_size_limit(self):
        with patch.object(bridge,'_connect') as create,self.live() as live:
            live._lock.acquire()
            try:self.assertEqual(self.request(live)[0],502)
            finally:live._lock.release()
            with patch.object(bridge,'MAX_BODY_BYTES',5):self.assertEqual(self.request(live,body=b'too-large')[0],502)
            create.assert_not_called()
    def test_http_error_preserves_body_status_and_turn_state_not_cookie(self):
        body=b'{"error":{"type":"rate_limit"}}'
        r=Response(body,429,[('Content-Type','application/json'),('Content-Length',str(len(body))),('X-Codex-Turn-State','opaque-test-only'),('Set-Cookie','must-not-forward')])
        observed=[]
        with patch.object(bridge,'_connect',return_value=Connection(r)),self.live(after=lambda *a:observed.append(a)) as live:
            status,actual,headers=self.request(live)
            self.assertEqual(status,429);self.assertEqual(actual,body)
            h=dict((k.lower(),v) for k,v in headers);self.assertEqual(h['x-codex-turn-state'],'opaque-test-only');self.assertNotIn('set-cookie',h)
            self.assertEqual(observed[0][-1],429);self.assertEqual(live.report()['status_counts'],{'429':1})
    def test_static_rejection_codes(self):
        checks=[({'Content-Type':'application/json'},'request_auth_missing'),
            ({'Content-Type':'application/json','Authorization':'Basic private'},'request_auth_invalid'),
            ({'Content-Type':'application/json','Authorization':'Bearer x','Host':'bad'},'request_host_mismatch'),
            ({'Content-Type':'text/plain','Authorization':'Bearer x'},'request_content_type_invalid'),
            ({'Content-Type':'application/json','Authorization':'Bearer x','Content-Length':'no'},'request_content_length_invalid'),
            ({'Content-Type':'application/json','Authorization':'Bearer x','X-Unknown-Private':'secret'},'request_header_unknown')]
        for h,code in checks:
            with patch.object(bridge,'_connect') as create,self.live() as live:
                self.assertEqual(self.request(live,headers=h)[0],502);create.assert_not_called()
                self.assertIn(code,live.report()['error_counts'])
    def test_unknown_header_diagnostic_never_echoes_names_or_values(self):
        h={'Content-Type':'application/json','Authorization':'Bearer private-value','X-OpenAI-Encrypted-Tool-Arguments':'private-mode','X-Unknown-PrivateName':'private-value'}
        with patch.object(bridge,'_connect') as create,self.live() as live:
            self.assertEqual(self.request(live,headers=h)[0],502);create.assert_not_called()
            d=live.report()['header_diagnostics'][0]
            self.assertEqual(d['unexpected_known_header_presence'],{'x-openai-encrypted-tool-arguments':True})
            self.assertEqual(d['unexpected_unknown_header_count'],1)
            text=json.dumps(live.report());self.assertNotIn('private-value',text);self.assertNotIn('privatename',text.casefold())
    def test_probe_never_opens_upstream_or_calls_capture(self):
        with patch.object(bridge,'_connect') as create,patch.object(bridge,'upstream_error_diagnostics') as diag:
            before=unittest.mock.Mock();after=unittest.mock.Mock()
            with self.live(probe_only=True,before=before,after=after,max_requests=1) as live:
                self.assertEqual(self.request(live)[0],503)
                self.assertEqual(self.request(live)[0],502)
                r=live.report();self.assertTrue(r['probe_only']);self.assertEqual(r['probe_valid_requests'],1);self.assertEqual(r['upstream_requests'],0)
            create.assert_not_called();diag.assert_not_called();before.assert_not_called();after.assert_not_called()
    def test_verified_sdk_session_headers_forward_exactly_without_logging_values(self):
        h={'Content-Type':'application/json','Authorization':'Bearer synthetic-only',
           'Session-Id':'private-session','Thread-Id':'private-thread','X-Client-Request-Id':'private-request'}
        conn=Connection()
        with patch.object(bridge,'_connect',return_value=conn),self.live() as live:
            self.assertEqual(self.request(live,headers=h)[0],200)
            d=live.report()['header_diagnostics'][0]
            self.assertEqual(d['unexpected_known_header_presence'],{})
            self.assertEqual(d['unexpected_unknown_header_count'],0)
            self.assertEqual(d['unexpected_known_header_source_sha256'],{})
            forwarded=conn.calls[0][1]['headers']
            self.assertEqual(forwarded['session-id'],'private-session')
            self.assertEqual(forwarded['thread-id'],'private-thread')
            self.assertEqual(forwarded['x-client-request-id'],'private-request')
            self.assertNotIn('private-',json.dumps(live.report()))
    def test_error_metadata_is_closed_and_quota_is_only_a_signal(self):
        b=json.dumps({'error':{'type':'permission_error','message':'Credential only authorized for use with Claude Code. Never echo private-token-123.'},'request_id':'private-id'}).encode()
        result=bridge.upstream_error_diagnostics(b,'application/json',403)
        self.assertEqual(result['provider_error_type'],'permission_error')
        self.assertTrue(result['message_signals']['claude_code_credential_scope'])
        self.assertFalse(result['message_signals']['quota_terms'])
        self.assertNotIn('private',json.dumps(result))
        x=bridge.upstream_error_diagnostics(b'{"error":{"type":"private-type","message":"quota"}}','application/json',429)
        self.assertEqual(x['provider_error_type'],'other_or_unknown');self.assertTrue(x['message_signals']['quota_terms'])
        self.assertNotIn('private-type',json.dumps(x))
    def test_malformed_error_metadata_fails_closed(self):
        for b in (b'invalid',b'{"error":{},"error":{"message":"private"}}',b'['*1000+b']'*1000,b'x'*65537):
            d=bridge.upstream_error_diagnostics(b,'application/json',403)
            self.assertEqual(d['provider_error_type'],'other_or_unknown');self.assertEqual(d['message_signals'],{})
        self.assertIsNone(bridge.upstream_error_diagnostics(b'{}','application/json',200))


if __name__=='__main__':unittest.main()
