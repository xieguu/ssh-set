"""Parser for the SSH command line accepted by Nexus SSH.

The parser deliberately accepts only SSH syntax used by the embedded executor.
It never invokes a shell, so pasted metacharacters cannot become local commands.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import shlex


class SSHCommandError(ValueError):
    """Raised when a pasted SSH command cannot be parsed safely."""


@dataclass(frozen=True)
class DynamicForward:
    bind_host: str
    bind_port: int

    @property
    def display(self) -> str:
        host = f"[{self.bind_host}]" if ":" in self.bind_host else self.bind_host
        return f"{host}:{self.bind_port}"


@dataclass(frozen=True)
class SSHCommand:
    original: str
    host: str
    username: str
    port: int = 22
    identity_file: str = ""
    dynamic_forwards: tuple[DynamicForward, ...] = ()
    no_remote_command: bool = False
    force_tty: bool = False
    remote_command: str = ""
    connect_timeout: float = 10.0
    server_alive_interval: int = 0

    @property
    def destination(self) -> str:
        return f"{self.username}@{self.host}" if self.username else self.host

    @property
    def summary(self) -> str:
        parts = [f"目标 {self.destination}:{self.port}"]
        if self.dynamic_forwards:
            bindings = ", ".join(item.display for item in self.dynamic_forwards)
            parts.append(f"SOCKS5 {bindings}")
        if self.remote_command:
            parts.append(f"远端命令 {self.remote_command}")
        elif self.no_remote_command:
            parts.append("仅转发，不打开 shell")
        else:
            parts.append("交互式 shell")
        if self.server_alive_interval:
            parts.append(f"保活 {self.server_alive_interval}s")
        return " · ".join(parts)


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _port(value: str, label: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise SSHCommandError(f"{label}必须是数字: {value}") from exc
    if not 1 <= result <= 65535:
        raise SSHCommandError(f"{label}范围必须是 1-65535")
    return result


def _dynamic_forward(value: str) -> DynamicForward:
    value = value.strip()
    if not value:
        raise SSHCommandError("-D 缺少监听端口")
    if value.startswith("["):
        close = value.find("]")
        if close < 0 or close + 1 >= len(value) or value[close + 1] != ":":
            raise SSHCommandError(f"无法解析 -D 地址: {value}")
        bind_host = value[1:close]
        port_text = value[close + 2 :]
    elif ":" in value:
        bind_host, port_text = value.rsplit(":", 1)
    else:
        bind_host, port_text = "127.0.0.1", value
    bind_host = bind_host.strip() or "127.0.0.1"
    if bind_host == "*":
        bind_host = "0.0.0.0"
    if any(char.isspace() for char in bind_host):
        raise SSHCommandError(f"无效的 -D 监听地址: {bind_host}")
    return DynamicForward(bind_host=bind_host, bind_port=_port(port_text, "动态转发端口"))


def _option_value(tokens: list[str], index: int, flag: str) -> tuple[str, int]:
    token = tokens[index]
    if token == flag:
        if index + 1 >= len(tokens):
            raise SSHCommandError(f"{flag} 缺少参数")
        return tokens[index + 1], index + 2
    return token[len(flag) :], index + 1


def parse_ssh_command(command_line: str) -> SSHCommand:
    """Parse a supported OpenSSH command without executing local shell syntax."""
    command_line = command_line.strip()
    if not command_line:
        raise SSHCommandError("请输入 SSH 命令")
    try:
        tokens = [_unquote(item) for item in shlex.split(command_line, posix=False)]
    except ValueError as exc:
        raise SSHCommandError(f"命令引号不完整: {exc}") from exc
    if not tokens:
        raise SSHCommandError("请输入 SSH 命令")
    executable = os.path.basename(tokens[0]).lower()
    if executable not in ("ssh", "ssh.exe"):
        raise SSHCommandError("解析器只允许 ssh 命令")

    port = 22
    login_name = ""
    identity_file = ""
    dynamic_forwards: list[DynamicForward] = []
    no_remote_command = False
    force_tty = False
    connect_timeout = 10.0
    server_alive_interval = 0
    destination = ""
    remote_tokens: list[str] = []

    index = 1
    while index < len(tokens):
        token = tokens[index]
        if destination:
            remote_tokens = tokens[index:]
            break
        if token == "--":
            index += 1
            if index >= len(tokens):
                raise SSHCommandError("缺少 SSH 目标主机")
            destination = tokens[index]
            index += 1
            continue
        if not token.startswith("-") or token == "-":
            destination = token
            index += 1
            continue
        if token in ("-N",):
            no_remote_command = True
            index += 1
            continue
        if token in ("-T",):
            force_tty = False
            index += 1
            continue
        if token in ("-t", "-tt"):
            force_tty = True
            index += 1
            continue
        if token in ("-4", "-6") or token.lstrip("-") in ("v", "vv", "vvv"):
            index += 1
            continue
        if token == "-D" or token.startswith("-D"):
            value, index = _option_value(tokens, index, "-D")
            dynamic_forwards.append(_dynamic_forward(value))
            continue
        if token == "-p" or token.startswith("-p"):
            value, index = _option_value(tokens, index, "-p")
            port = _port(value, "SSH 端口")
            continue
        if token == "-l" or token.startswith("-l"):
            login_name, index = _option_value(tokens, index, "-l")
            continue
        if token == "-i" or token.startswith("-i"):
            identity_file, index = _option_value(tokens, index, "-i")
            continue
        if token == "-o" or token.startswith("-o"):
            value, index = _option_value(tokens, index, "-o")
            if "=" not in value:
                raise SSHCommandError("-o 参数应写成 Name=Value")
            name, option_value = value.split("=", 1)
            option_name = name.lower()
            if option_name == "connecttimeout":
                try:
                    connect_timeout = float(option_value)
                except ValueError as exc:
                    raise SSHCommandError("ConnectTimeout 必须是数字") from exc
                if connect_timeout <= 0 or connect_timeout > 300:
                    raise SSHCommandError("ConnectTimeout 范围必须是 0-300 秒")
            elif option_name == "serveraliveinterval":
                try:
                    server_alive_interval = int(option_value)
                except ValueError as exc:
                    raise SSHCommandError("ServerAliveInterval 必须是整数秒数") from exc
                if server_alive_interval < 0 or server_alive_interval > 86400:
                    raise SSHCommandError("ServerAliveInterval 范围必须是 0-86400 秒")
            else:
                raise SSHCommandError(f"当前执行器不支持 -o {name}")
            continue
        if token.startswith(("-L", "-R", "-J", "-W", "-F", "-S")):
            raise SSHCommandError(f"当前版本尚未支持参数 {token[:2]}")
        raise SSHCommandError(f"不支持的 SSH 参数: {token}")

    if not destination:
        raise SSHCommandError("缺少 SSH 目标主机")
    if destination.startswith("-"):
        raise SSHCommandError("SSH 目标主机无效")
    if "@" in destination:
        destination_user, host = destination.rsplit("@", 1)
        username = destination_user or login_name
    else:
        host = destination
        username = login_name
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or any(char.isspace() for char in host):
        raise SSHCommandError("SSH 目标主机无效")
    if not username:
        raise SSHCommandError("命令中缺少用户名，请使用 user@host 或 -l user")

    remote_command = ""
    if remote_tokens:
        # OpenSSH sends the remaining argv items as one space-joined command
        # string. Quoting has already been consumed by the local parser.
        remote_command = " ".join(remote_tokens)
        no_remote_command = False

    return SSHCommand(
        original=command_line,
        host=host,
        username=username,
        port=port,
        identity_file=identity_file,
        dynamic_forwards=tuple(dynamic_forwards),
        no_remote_command=no_remote_command,
        force_tty=force_tty,
        remote_command=remote_command,
        connect_timeout=connect_timeout,
        server_alive_interval=server_alive_interval,
    )
