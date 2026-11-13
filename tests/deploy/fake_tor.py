"""A stand-in for Tor's SOCKS socket, which sends each request to this machine."""

import contextlib
import socket
import socketserver
import threading
from pathlib import Path


def serve_socks(path: Path) -> None:
    """Serve SOCKS5 on a Unix socket, and connect each request to 127.0.0.1.

    The port is the one asked for, so a request for the live site reaches this
    machine's nginx. curl always offers no login, so none is asked for.
    """
    path.unlink(missing_ok=True)
    server = socketserver.ThreadingUnixStreamServer(str(path), Socks)
    server.daemon_threads = True
    path.chmod(0o666)
    threading.Thread(target=server.serve_forever, daemon=True).start()


class Socks(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        """Answer one SOCKS5 handshake, then relay the connection both ways."""
        client = self.request
        client.recv(client.recv(2)[1])
        client.sendall(b"\x05\x00")
        kind = client.recv(4)[3]
        if kind == 3:  # a host name
            client.recv(client.recv(1)[0])
        else:
            client.recv(4 if kind == 1 else 16)
        port = int.from_bytes(client.recv(2), "big")
        upstream = socket.create_connection(("127.0.0.1", port))
        client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        threading.Thread(target=relay, args=(client, upstream), daemon=True).start()
        relay(upstream, client)


def relay(source: socket.socket, sink: socket.socket) -> None:
    with source, sink, contextlib.suppress(OSError):
        while data := source.recv(65536):
            sink.sendall(data)
