from pathlib import Path
import sys
import urllib.error

import pytest

sys.path.insert(0,str(Path(__file__).parents[1] / 'scripts'))
from amazon_us_worker import HttpFirstAdapter, DEFAULTS
from recovery_scheduler import RecoveryDenied


def test_new_action_retires_old_browser_and_relay_before_rebinding_lease():
    from types import SimpleNamespace
    closed=[]
    adapter=HttpFirstAdapter({**DEFAULTS,'_recovery_before_request':lambda:1})
    adapter.browser=SimpleNamespace(close=lambda:closed.append('browser'))
    adapter._proxy_relay=SimpleNamespace(close=lambda:closed.append('relay'))
    try:
        adapter.begin_action()
        assert closed==['browser','relay']
        assert adapter.browser is None and adapter._proxy_relay is None
    finally:
        adapter.close()


def test_http_inner_retry_must_reauthorize_before_second_network_request():
    charges = []
    requests = []
    def authorize():
        charges.append(1)
        if len(charges) > 1:
            raise RecoveryDenied('recovery_budget_or_lease_denied')
        return 1.0
    config = {**DEFAULTS, 'http_retry_backoff_seconds': 0, '_recovery_before_request': authorize}
    adapter = HttpFirstAdapter(config)
    class OfflineTransport:
        def open(self, request, timeout):
            requests.append(request)
            raise urllib.error.URLError('offline fixture')
    adapter.opener = OfflineTransport()
    try:
        with pytest.raises(RecoveryDenied):
            adapter.fetch('https://www.amazon.com/dp/B0CC2FRY3J')
        assert len(requests) == 1
        assert len(charges) == 2
    finally:
        adapter.close()


def test_exhausted_transport_does_not_publish_zero_as_known_total_bytes():
    from amazon_us_worker import AdapterFetchError
    class FailedTransport:
        def open(self,*args,**kwargs): raise urllib.error.URLError('offline transport fixture')
    adapter=HttpFirstAdapter({**DEFAULTS,'http_max_attempts':1})
    adapter.opener=FailedTransport()
    try:
        with pytest.raises(AdapterFetchError): adapter.fetch('https://fixture.invalid/probe')
        assert adapter.last_transfer_bytes is None
    finally:
        adapter.close()


def test_recovery_http_response_has_a_finite_body_read_budget():
    reads = []
    class Response:
        headers = {}
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def getcode(self): return 200
        def read(self, size=-1):
            reads.append(size)
            return b'x'*(4*1024*1024+1)
    class Transport:
        def open(self,*args,**kwargs): return Response()
    adapter = HttpFirstAdapter({**DEFAULTS,'_recovery_before_request':lambda: 1})
    adapter.opener = Transport()
    try:
        with pytest.raises(RecoveryDenied):
            adapter.fetch('https://www.amazon.com/dp/B0CC2FRY3J')
        assert reads == [4*1024*1024+1]
    finally:
        adapter.close()


@pytest.mark.parametrize('slow_part',['headers','body'])
@pytest.mark.parametrize('late_binding',[False,True])
def test_absolute_http_budget_stops_slow_trickle_headers_and_body(slow_part,late_binding):
    import threading,time
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            chunks = [b'HTTP/1.1 200 OK\r\nContent-Length: 1\r\nX-Slow: a',b'b',b'c\r\n\r\nx'] if slow_part=='headers' else [b'HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nx',b'x',b'x']
            try:
                for index,chunk in enumerate(chunks):
                    if index: time.sleep(0.7)
                    self.wfile.write(chunk); self.wfile.flush()
            except OSError: pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    config={**DEFAULTS,'proxy_url':f'http://127.0.0.1:{server.server_port}',
            'request_timeout_seconds':1,'http_max_attempts':1}
    if not late_binding: config['_recovery_before_request']=lambda:1
    adapter=HttpFirstAdapter(config)
    if late_binding: adapter.config['_recovery_before_request']=lambda:1
    start=time.monotonic()
    try:
        with pytest.raises(RecoveryDenied):
            adapter.fetch('http://fixture.invalid/probe')
        assert time.monotonic()-start < 1.3
    finally:
        adapter.close(); server.shutdown(); server.server_close(); thread.join(2)


def test_production_pool_builds_slot_after_recovery_hooks_are_bound():
    import threading,time
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from proxy_session_pool import ProxySessionPool
    from amazon_us_worker import classify_block
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            try:
                self.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Length: 1\r\nX-Slow: a'); self.wfile.flush()
                time.sleep(0.7); self.wfile.write(b'b'); self.wfile.flush()
                time.sleep(0.7); self.wfile.write(b'c\r\n\r\nx'); self.wfile.flush()
            except OSError: pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    config={**DEFAULTS,'proxy_url':f'http://127.0.0.1:{server.server_port}',
            'proxy_session_ports':[server.server_port],'request_timeout_seconds':1}
    pool=ProxySessionPool(config,HttpFirstAdapter,classify_block)
    pool.configure_capacity_reservation(['session-01'],lambda: {'status':'active'})
    pool.begin_run('fixture','tenant','worker')
    pool.begin_action()
    pool.config['_recovery_before_request']=lambda:1
    start=time.monotonic()
    try:
        with pytest.raises(RecoveryDenied): pool.fetch('http://fixture.invalid/dp/B0CC2FRY3J')
        assert time.monotonic()-start < 1.3
    finally:
        pool.close(); server.shutdown(); server.server_close(); thread.join(2)


@pytest.mark.parametrize('late_binding',[False,True])
def test_deadline_keeps_verified_tls_and_connect_only_auth_on_loopback(tmp_path,monkeypatch,late_binding):
    import base64,select,shutil,socket,socketserver,ssl,subprocess,threading
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from proxy_tunnel_auth import ProxyTunnelAuthHTTPSHandler
    openssl=shutil.which('openssl') or str(Path(shutil.which('git')).parents[1]/'usr/bin/openssl.exe')
    assert Path(openssl).is_file()
    key,cert=tmp_path/'key.pem',tmp_path/'cert.pem'
    subprocess.run([openssl,'req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),
                    '-days','1','-subj','/CN=fixture.invalid','-addext','subjectAltName=DNS:fixture.invalid'],capture_output=True,check=True)
    origin_headers=[]; connect_headers=[]
    class Origin(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            origin_headers.append(dict(self.headers))
            self.send_response(200); self.send_header('Content-Length','2'); self.end_headers(); self.wfile.write(b'ok')
    origin=ThreadingHTTPServer(('127.0.0.1',0),Origin)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert,key)
    origin.socket=context.wrap_socket(origin.socket,server_side=True)
    class Proxy(socketserver.BaseRequestHandler):
        def handle(self):
            data=b''
            while b'\r\n\r\n' not in data: data+=self.request.recv(4096)
            connect_headers.append(data)
            with socket.create_connection(('127.0.0.1',origin.server_port),timeout=3) as upstream:
                self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                while True:
                    ready,_,_=select.select((self.request,upstream),(),(),3)
                    if not ready: return
                    for source in ready:
                        payload=source.recv(65536)
                        if not payload: return
                        (upstream if source is self.request else self.request).sendall(payload)
    proxy=socketserver.ThreadingTCPServer(('127.0.0.1',0),Proxy)
    threads=[threading.Thread(target=value.serve_forever,daemon=True) for value in (origin,proxy)]
    for thread in threads: thread.start()
    monkeypatch.setenv('TEST_PROXY_USER','fixture-user'); monkeypatch.setenv('TEST_PROXY_PASS','fixture-pass')
    config={**DEFAULTS,'proxy_url':f'http://127.0.0.1:{proxy.server_address[1]}',
            'proxy_username_env':'TEST_PROXY_USER','proxy_password_env':'TEST_PROXY_PASS'}
    if not late_binding: config['_recovery_before_request']=lambda:2
    adapter=HttpFirstAdapter(config)
    if late_binding: adapter.config['_recovery_before_request']=lambda:2
    for handler in adapter._opener_handlers:
        if isinstance(handler,ProxyTunnelAuthHTTPSHandler): handler._context=ssl.create_default_context(cafile=str(cert))
    adapter._rebuild_opener()
    try:
        assert adapter.fetch('https://fixture.invalid/probe') == ('ok',200)
        assert b'Proxy-Authorization: Basic '+base64.b64encode(b'fixture-user:fixture-pass') in connect_headers[0]
        assert not any(name.lower()=='proxy-authorization' for name in origin_headers[0])
    finally:
        adapter.close()
        for server in (proxy,origin): server.shutdown(); server.server_close()
        for thread in threads: thread.join(2)
        key.unlink(missing_ok=True); cert.unlink(missing_ok=True)
