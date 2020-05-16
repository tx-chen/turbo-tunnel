# -*- coding: utf-8 -*-
'''Tunnel
'''

import asyncio
import re
import socket
import time

import tornado.iostream

from . import registry
from . import utils


class Tunnel(utils.IStream):
    '''Tunnel base class
    '''
    timeout = 15

    def __init__(self, tunnel, url, address):
        self._tunnel = tunnel
        self._url = url
        self._addr, self._port = address or (None, None)
        if (not self._addr or not self._port) and url:
            self._addr, self._port = url.host, url.port
        assert(self._addr and self._port)
        self._running = True
        self._connected = False

    def __str__(self):
        address = self._url
        if not address:
            address = '%s:%d' % (self._addr, self._port)
        return '%s %s' % (self.__class__.__name__, address)

    @classmethod
    def has_cache(cls, url):
        return False

    @property
    def socket(self):
        if self._tunnel:
            return self._tunnel.socket
        return None

    def on_read(self, buffer):
        utils.logger.debug('[%s] Recv %d bytes from upstream' %
                           (self.__class__.__name__, len(buffer)))

    def on_close(self):
        address = ''
        if self._addr and self._port and (self._addr != self._url.host
                                          or self._port != self._url.port):
            address = '/%s/%d' % (self._addr, self._port)
        utils.logger.warn('[%s] Upstream %s%s closed' %
                          (self.__class__.__name__, self._url, address))
        self.close()

    async def wait_for_connecting(self):
        time0 = time.time()
        while time.time() - time0 < self.__class__.timeout:
            if not self._connected:
                await tornado.gen.sleep(0.01)
            else:
                break
        else:
            raise utils.TimeoutError('Wait for connecting timeout')


class TCPTunnel(Tunnel):
    '''TCP Tunnel
    '''
    def __init__(self, tunnel, url=None, address=None, server_side=False):
        self._stream = None
        if isinstance(tunnel, socket.socket):
            self._stream = tornado.iostream.IOStream(tunnel)
        elif isinstance(tunnel, tornado.iostream.IOStream):
            self._stream = tunnel
        elif not isinstance(tunnel, Tunnel):
            raise ValueError('Invalid param: %r' % tunnel)
        if self._stream and not address:
            if server_side:
                address = self._stream.socket.getsockname()
            else:
                address = self._stream.socket.getpeername()
        super(TCPTunnel, self).__init__(tunnel, url, address)

    def __getattr__(self, attr):
        if self._stream:
            try:
                return getattr(self._stream, attr)
            except AttributeError:
                raise AttributeError("'%s' object has no attribute '%s'" %
                                     (self.__class__.__name__, attr))
        else:
            raise AttributeError("'%s' object has no attribute '%s'" %
                                 (self.__class__.__name__, attr))

    @property
    def socket(self):
        if self._stream:
            return self._stream.socket
        elif self._tunnel:
            return self._tunnel.socket
        else:
            return None

    @property
    def stream(self):
        return self._stream

    async def connect(self):
        if self._stream:
            try:
                return await self._stream.connect((self._addr, self._port))
            except tornado.iostream.StreamClosedError:
                raise utils.TunnelConnectError('Connect %s:%d failed' %
                                               (self._addr, self._port))
        return True

    async def read(self):
        if self._stream:
            try:
                buffer = await self._stream.read_bytes(4096, partial=True)
            except tornado.iostream.StreamClosedError:
                pass
            else:
                if buffer:
                    return buffer
            raise utils.TunnelClosedError(self)
        elif self._tunnel:
            return await self._tunnel.read()
        else:
            raise utils.TunnelClosedError(self)

    async def write(self, buffer):
        if self._stream:
            return await self._stream.write(buffer)
        elif self._tunnel:
            return await self._tunnel.write(buffer)
        else:
            raise utils.TunnelClosedError(self)

    def close(self):
        utils.logger.debug('[%s] %s closed' % (self.__class__.__name__, self))
        if self._stream:
            self._stream.close()
            self._stream = None
        elif self._tunnel:
            self._tunnel.close()
            self._tunnel = None


class TunnelIOStream(tornado.iostream.BaseIOStream):
    '''Tunnel to IOStream
    '''
    def __init__(self, tunnel):
        super(TunnelIOStream, self).__init__()
        self._tunnel = tunnel
        self._buffer = b''
        self._close_callback = None
        asyncio.ensure_future(self.transfer_data_task())

    async def transfer_data_task(self):
        while self._tunnel:
            try:
                buffer = await self._tunnel.read()
            except utils.TunnelClosedError:
                break
            if buffer:
                self._buffer += buffer
            else:
                break

    async def read_bytes(self, num_bytes, partial=False):
        while self._tunnel:
            if len(self._buffer) >= num_bytes:
                buffer = self._buffer[:num_bytes]
                self._buffer = self._buffer[num_bytes:]
                return buffer
            elif partial and self._buffer:
                buffer = self._buffer
                self._buffer = b''
                return buffer
            await asyncio.sleep(0.001)
        else:
            raise utils.TunnelClosedError(self)

    async def read_until_regex(self, regex, max_bytes=None):
        read_regex = re.compile(regex)
        while self._tunnel:
            m = read_regex.search(self._buffer)
            if m is not None:
                buffer = self._buffer[:m.end()]
                self._buffer = self._buffer[m.end():]
                return buffer
            await asyncio.sleep(0.001)
        else:
            raise utils.TunnelClosedError(self)

    def set_close_callback(self, callback):
        self._close_callback = callback

    def close(self, exc_info=False):
        if self._close_callback:
            self._close_callback()
        if self._tunnel:
            self._tunnel.close()
            self._tunnel = None

    def read_from_fd(self, buf):
        if not self._buffer:
            return None
        elif len(buf) >= len(self._buffer):
            buf[:] = self._buffer
            read_size = len(self._buffer)
            self._buffer = b''
            return read_size
        else:
            array_size = len(buf)
            buf[:] = self._buffer[:array_size]
            self._buffer = self._buffer[array_size:]
            return array_size

    def write_to_fd(self, data):
        asyncio.ensure_future(self._tunnel.write(data))
        return len(data)


class TunnelTransport(asyncio.Transport):
    '''Tunnel to Transport
    '''
    def __init__(self, tunnel, handler):
        super(TunnelTransport, self).__init__()
        self._tunnel = tunnel
        self._handler = handler
        self._extra['socket'] = self._tunnel.socket
        self._extra['sockname'] = self._tunnel.socket.getsockname()
        self._extra['peername'] = self._tunnel.socket.getpeername()
        asyncio.ensure_future(self.transfer_data_task())

    def write(self, data):
        asyncio.ensure_future(self._tunnel.write(data))

    def abort(self):
        self._tunnel.close()

    async def transfer_data_task(self):
        while True:
            try:
                buffer = await self._tunnel.read()
            except utils.TunnelClosedError:
                break
            if buffer:
                self._handler.data_received(buffer)
            else:
                break


registry.tunnel_registry.register('tcp', TCPTunnel)
