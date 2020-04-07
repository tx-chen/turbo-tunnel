# -*- coding: utf-8 -*-
'''HTTPS Tunnel
'''

import asyncio

import tornado.iostream
import tornado.web

from . import chain
from . import registry
from . import server
from . import tunnel
from . import utils


class HTTPSTunnel(tunnel.Tunnel):
    '''HTTPS Tunnel
    '''
    def __init__(self, tunnel, url, address):
        super(HTTPSTunnel, self).__init__(tunnel, url, address)

    @property
    def socket(self):
        return self._tunnel.socket

    @property
    def stream(self):
        return self._tunnel.stream

    async def connect(self):
        data = 'CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n' % (
            self._addr, self._port, self._addr, self._port)
        await self._tunnel.write(data.encode())
        buffer = b''
        while True:
            buffer += await self._tunnel.read()
            if buffer.endswith(b'\r\n\r\n'):
                break

        lines = buffer.strip().split(b'\r\n')
        items = lines[0].split()
        code = int(items[1])
        reason = (b' '.join(items[2:])).decode()
        if code == 200:
            return True
        utils.logger.warn('[%s] Connect %s:%d over %s failed: [%d] %s' %
                          (self.__class__.__name__, self._addr, self._port,
                           self._url, code, reason))
        return False

    async def read(self):
        return await self._tunnel.read()

    async def write(self, buffer):
        await self._tunnel.write(buffer)

    def close(self):
        if self._tunnel:
            self._tunnel.close()
            self._tunnel = None


class DefaultHandler(tornado.web.RequestHandler):
    pass


class HTTPRouter(tornado.routing.Router):
    '''Support CONNECT method
    '''
    def __init__(self, app, handlers=None):
        self._app = app
        self._handlers = handlers or []

    def find_handler(self, request, **kwargs):
        handler = DefaultHandler
        if request.method == "CONNECT":
            for methods, _, _handler in self._handlers:
                if 'CONNECT' in methods:
                    handler = _handler
                    break
        else:
            for methods, pattern, _handler in self._handlers:
                if request.method not in methods and '*' not in methods:
                    continue
                if not re.match(pattern, request.path): continue
                handler = _handler
                break

        return self._app.get_handler_delegate(
            request, handler)  #, path_args=[request.path]


class HTTPSTunnelServer(server.TunnelServer):
    '''HTTPS Tunnel Server
    '''
    def post_init(self):
        this = self

        class HTTPServerHandler(tornado.web.RequestHandler):
            '''HTTP Server Handler
            '''
            SUPPORTED_METHODS = ['CONNECT']

            async def connect(self):
                address = self.request.path.split(':')
                address[1] = int(address[1])
                address = tuple(address)
                downstream = utils.TCPStream(self.request.connection.detach())
                with server.TunnelConnection(
                        self.request.connection.context.address,
                        address) as tun_conn:
                    with this.create_tunnel_chain() as tunnel_chain:
                        try:
                            await tunnel_chain.create_tunnel(address)
                        except utils.TunnelError as e:
                            if not isinstance(e, utils.TunnelBlockedError):
                                utils.logger.warn(
                                    '[%s] Connect %s:%d failed: %s' %
                                    (self.__class__.__name__, address[0],
                                     address[1], e))
                            if not downstream.closed():
                                if isinstance(e, utils.TunnelBlockedError):
                                    await downstream.write(
                                        b'HTTP/1.1 403 Forbidden\r\n\r\n')
                                else:
                                    await downstream.write(
                                        b'HTTP/1.1 504 Gateway timeout\r\n\r\n'
                                    )
                            else:
                                tun_conn.on_downstream_closed()
                            self._finished = True
                            return
                        else:
                            await downstream.write(
                                b'HTTP/1.1 200 HTTPSTunnel Established\r\n\r\n'
                            )
                        tasks = [
                            this.forward_data_to_upstream(
                                tun_conn, downstream, tunnel_chain.tail),
                            this.forward_data_to_downstream(
                                tun_conn, downstream, tunnel_chain.tail)
                        ]
                        await asyncio.wait(tasks,
                                           return_when=asyncio.FIRST_COMPLETED)

                        downstream.close()
                        self._finished = True

        handlers = [
            (['CONNECT'], r'', HTTPServerHandler),
        ]
        app = tornado.web.Application()
        router = HTTPRouter(app, handlers)
        self._http_server = tornado.httpserver.HTTPServer(router)

    def start(self):
        self._http_server.listen(self._listen_url.port, self._listen_url.host)
        utils.logger.info('[%s] HTTP server is listening on %s:%d' %
                          (self.__class__.__name__, self._listen_url.host,
                           self._listen_url.port))


registry.tunnel_registry.register('http', HTTPSTunnel)
registry.tunnel_registry.register('https', HTTPSTunnel)
registry.server_registry.register('http', HTTPSTunnelServer)
registry.server_registry.register('https', HTTPSTunnelServer)