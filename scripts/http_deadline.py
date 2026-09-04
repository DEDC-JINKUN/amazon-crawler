"""Absolute transport deadline for urllib connections, including header trickles.

Only shuts down owned sockets; never terminates TLS or changes request identity.
"""
from __future__ import annotations

import http.client
import socket
import threading
import time
import urllib.request

from recovery_scheduler import RecoveryDenied


class _DeadlineSocket:
    """Keep native socket/TLS behavior, but cap every buffered read by one clock."""
    def __init__(self,sock,guard):
        self._socket=sock
        self._guard=guard

    def __getattr__(self,name):
        return getattr(self._socket,name)

    def gettimeout(self):
        self._guard.check()
        timeout=self._socket.gettimeout()
        remaining=self._guard.deadline-time.monotonic()
        return min(timeout,remaining) if timeout is not None else remaining

    def sendall(self,data,*args,**kwargs):
        self._socket.settimeout(self.gettimeout())
        return self._socket.sendall(data,*args,**kwargs)

    def makefile(self,*args,**kwargs):
        handle=self._socket.makefile(*args,**kwargs)
        raw=getattr(handle,'raw',handle)
        original_readinto=raw.readinto
        def readinto(buffer):
            self._socket.settimeout(self.gettimeout())
            try:
                value=original_readinto(buffer)
            except OSError:
                self._guard.check()
                raise
            self._guard.check()
            return value
        raw.readinto=readinto
        return handle


class HttpDeadline:
    def __init__(self, seconds):
        self.deadline = time.monotonic()+float(seconds)
        self._connections = []
        self._sockets = []
        self._lock = threading.Lock()
        self._timer = threading.Timer(max(0,float(seconds)),self._expire)
        self._timer.daemon = True
        self._timer.start()

    @property
    def expired(self):
        return time.monotonic() >= self.deadline

    def check(self):
        if self.expired:
            raise RecoveryDenied('recovery_http_deadline_exceeded')

    def bind_connection(self, connection):
        self.check()
        with self._lock:
            self._connections.append(connection)
        original_send = connection.send
        original_create = connection._create_connection
        original_connect = connection.connect
        def create(*args,**kwargs):
            self.check()
            sock=original_create(*args,**kwargs)
            try: self.check()
            except RecoveryDenied:
                sock.close()
                raise
            return _DeadlineSocket(sock,self)
        def connect():
            self.check()
            original_connect()
            self.check()
            if not isinstance(connection.sock,_DeadlineSocket):
                connection.sock=_DeadlineSocket(connection.sock,self)
        connection._create_connection=create
        connection.connect=connect
        def send(data):
            self.check()
            return original_send(data)
        connection.send = send

    def bind_response(self, response):
        value = response
        for _ in range(5):
            sock = getattr(value,'_sock',None)
            if sock is not None:
                with self._lock:
                    self._sockets.append(sock)
                break
            value = getattr(value,'fp',None) or getattr(value,'raw',None)
            if value is None: break
        self.check()

    def _expire(self):
        with self._lock:
            sockets = list(self._sockets)+[getattr(conn,'sock',None) for conn in self._connections]
        for sock in sockets:
            if sock is not None:
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass

    def close(self):
        self._timer.cancel()


class DeadlineHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, observer):
        super().__init__()
        self.observer=observer

    def http_open(self, request):
        def factory(host,**kwargs):
            conn=http.client.HTTPConnection(host,**kwargs)
            self.observer(conn)
            return conn
        return self.do_open(factory,request)


class DeadlineHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, observer):
        super().__init__()
        self.observer=observer

    def https_open(self, request):
        def factory(host,**kwargs):
            conn=http.client.HTTPSConnection(host,**kwargs)
            self.observer(conn)
            return conn
        return self.do_open(factory,request,context=self._context)
