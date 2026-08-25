"""Execution primitives for parsed SSH commands.

Dynamic forwarding is implemented as a local SOCKS5 CONNECT proxy whose remote
connections are opened through an authenticated Paramiko Transport.
"""

from __future__ import annotations

import socket
import socketserver
import struct
import threading
from collections.abc import Callable

import paramiko

from ssh_command import DynamicForward, SSHCommand


class SSHExecutionError(RuntimeError):
    """Raised when a parsed SSH operation cannot be started."""


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("SOCKS client closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class _SOCKS5Handler(socketserver.BaseRequestHandler):
    request: socket.socket

    def handle(self) -> None:
        server = self.server
        transport: paramiko.Transport = server.ssh_transport  # type: ignore[attr-defined]
        reporter: Callable[[str], None] = server.reporter  # type: ignore[attr-defined]
        channel: paramiko.Channel | None = None
        try:
            self.request.settimeout(15.0)
            version, method_count = _read_exact(self.request, 2)
            if version != 5:
                return
            methods = _read_exact(self.request, method_count)
            if 0 not in methods:
                self.request.sendall(b"\x05\xff")
                return
            self.request.sendall(b"\x05\x00")

            version, command, _reserved, address_type = _read_exact(self.request, 4)
            if version != 5 or command != 1:
                self.request.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            if address_type == 1:
                destination_host = socket.inet_ntoa(_read_exact(self.request, 4))
            elif address_type == 3:
                name_length = _read_exact(self.request, 1)[0]
                destination_host = _read_exact(self.request, name_length).decode("idna")
            elif address_type == 4:
                destination_host = socket.inet_ntop(socket.AF_INET6, _read_exact(self.request, 16))
            else:
                self.request.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            destination_port = struct.unpack("!H", _read_exact(self.request, 2))[0]
            source = (str(self.client_address[0]), int(self.client_address[1]))
            try:
                channel = transport.open_channel(
                    "direct-tcpip",
                    (destination_host, destination_port),
                    source,
                    timeout=15.0,
                )
            except Exception as exc:
                reporter(f"SOCKS 转发失败 {destination_host}:{destination_port}: {exc}")
                self.request.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            if channel is None:
                self.request.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            self.request.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            reporter(f"SOCKS {source[0]}:{source[1]} → {destination_host}:{destination_port}")
            self.request.settimeout(None)
            channel.settimeout(1.0)
            stopped = threading.Event()

            def local_to_remote() -> None:
                try:
                    while not stopped.is_set():
                        data = self.request.recv(65536)
                        if not data:
                            break
                        channel.sendall(data)
                except (ConnectionError, OSError, socket.timeout, EOFError):
                    pass
                finally:
                    stopped.set()

            def remote_to_local() -> None:
                try:
                    while not stopped.is_set():
                        data = channel.recv(65536)
                        if not data:
                            break
                        self.request.sendall(data)
                except (ConnectionError, OSError, socket.timeout, EOFError):
                    pass
                finally:
                    stopped.set()

            local_thread = threading.Thread(target=local_to_remote, name="socks-local-to-ssh", daemon=True)
            remote_thread = threading.Thread(target=remote_to_local, name="socks-ssh-to-local", daemon=True)
            local_thread.start()
            remote_thread.start()
            while not stopped.wait(0.1):
                pass
            try:
                self.request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            channel.close()
            local_thread.join(timeout=1.0)
            remote_thread.join(timeout=1.0)
        except (ConnectionError, OSError, EOFError):
            return
        finally:
            if channel is not None:
                channel.close()


class _ThreadedSOCKSServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _ThreadedSOCKSServerV6(_ThreadedSOCKSServer):
    address_family = socket.AF_INET6


class DynamicForwarder:
    def __init__(
        self,
        transport: paramiko.Transport,
        spec: DynamicForward,
        reporter: Callable[[str], None],
    ) -> None:
        self.transport = transport
        self.spec = spec
        self.reporter = reporter
        self.server: _ThreadedSOCKSServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        server_type = _ThreadedSOCKSServerV6 if ":" in self.spec.bind_host else _ThreadedSOCKSServer
        try:
            address = (
                (self.spec.bind_host, self.spec.bind_port, 0, 0)
                if server_type is _ThreadedSOCKSServerV6
                else (self.spec.bind_host, self.spec.bind_port)
            )
            server = server_type(address, _SOCKS5Handler)
        except OSError as exc:
            raise SSHExecutionError(f"无法监听 {self.spec.display}: {exc}") from exc
        server.ssh_transport = self.transport  # type: ignore[attr-defined]
        server.reporter = self.reporter  # type: ignore[attr-defined]
        self.server = server
        self.thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name=f"socks5-{self.spec.bind_port}",
            daemon=True,
        )
        self.thread.start()
        actual_port = int(server.server_address[1])
        actual_host = self.spec.bind_host
        if actual_host not in ("127.0.0.1", "::1", "localhost"):
            self.reporter(f"警告：SOCKS5 监听在 {actual_host}，局域网内其他设备可能可访问")
        if actual_port != self.spec.bind_port:
            self.reporter(f"SOCKS5 已监听 {actual_host}:{actual_port}")
        else:
            self.reporter(f"SOCKS5 已监听 {self.spec.display}")

    def stop(self) -> None:
        server, thread = self.server, self.thread
        self.server = None
        self.thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)


class SSHCommandExecutor:
    """Apply parsed command semantics to an established SSH connection."""

    def __init__(
        self,
        client: paramiko.SSHClient,
        transport: paramiko.Transport,
        reporter: Callable[[str], None],
    ) -> None:
        self.client = client
        self.transport = transport
        self.reporter = reporter
        self.forwarders: list[DynamicForwarder] = []
        self.channel: paramiko.Channel | None = None

    def start(self, command: SSHCommand) -> paramiko.Channel | None:
        try:
            for spec in command.dynamic_forwards:
                forwarder = DynamicForwarder(self.transport, spec, self.reporter)
                forwarder.start()
                self.forwarders.append(forwarder)
            if command.remote_command:
                channel = self.transport.open_session(timeout=command.connect_timeout)
                if command.force_tty:
                    channel.get_pty(term="xterm-256color", width=120, height=38)
                channel.exec_command(command.remote_command)
                self.channel = channel
            elif not command.no_remote_command:
                self.channel = self.client.invoke_shell(term="xterm-256color", width=120, height=38)
            if self.channel is not None:
                self.channel.settimeout(0.25)
            return self.channel
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        channel = self.channel
        self.channel = None
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass
        for forwarder in reversed(self.forwarders):
            forwarder.stop()
        self.forwarders.clear()
