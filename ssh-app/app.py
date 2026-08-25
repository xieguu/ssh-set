"""Nexus SSH - a small local graphical SSH client.

The application intentionally keeps credentials in memory only.  Saved profiles
contain connection metadata, but never passwords or key passphrases.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any

import paramiko
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from ssh_command import SSHCommand, SSHCommandError, parse_ssh_command
from ssh_executor import SSHCommandExecutor


APP_NAME = "Nexus SSH"
APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "NexusSSH"
PROFILE_FILE = APP_DIR / "profiles.json"
ANSI_ESCAPE = re.compile(r"\x1B(?:\][^\x07]*(?:\x07|\x1B\\)|\[[0-?]*[ -/]*[@-~]|[@-_])")


class RememberingHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Accept an unknown host key and retain its fingerprint for the UI.

    This keeps first-run setup practical for a local utility while making the
    trust decision visible to the operator.  Known host keys loaded by Paramiko
    are still used normally.
    """

    def __init__(self) -> None:
        self.fingerprint = ""

    def missing_host_key(
        self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey
    ) -> None:
        digest = hashlib.sha256(key.asbytes()).digest()
        self.fingerprint = "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")
        client._host_keys.add(hostname, key.get_name(), key)  # type: ignore[attr-defined]


class NexusSSH(tk.Tk):
    BG = "#0b1017"
    PANEL = "#111923"
    PANEL_2 = "#162231"
    BORDER = "#253447"
    TEXT = "#e8eef7"
    MUTED = "#8ea1b8"
    BLUE = "#4ea1ff"
    GREEN = "#39d98a"
    RED = "#ff6b7a"
    AMBER = "#f4bd62"
    TERMINAL_BG = "#080c11"

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1340x820")
        self.minsize(1050, 680)
        self.configure(bg=self.BG)

        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.io_lock = threading.Lock()
        self.client: paramiko.SSHClient | None = None
        self.channel: paramiko.Channel | None = None
        self.command_executor: SSHCommandExecutor | None = None
        self.parsed_ssh_command: SSHCommand | None = None
        self.active_ssh_command: SSHCommand | None = None
        self.connected_at: float | None = None
        self.current_fingerprint = ""
        self.command_history: list[str] = []
        self.history_index = 0
        self.profiles: list[dict[str, Any]] = []
        self.selected_profile_index: int | None = None

        self.profile_name = tk.StringVar(value="本地服务器")
        self.host = tk.StringVar()
        self.port = tk.StringVar(value="22")
        self.username = tk.StringVar()
        self.auth_mode = tk.StringVar(value="password")
        self.password = tk.StringVar()
        self.key_path = tk.StringVar()
        self.key_passphrase = tk.StringVar()
        self.connection_state = tk.StringVar(value="未连接")
        self.connection_detail = tk.StringVar(value="准备就绪")
        self.remote_host = tk.StringVar(value="--")
        self.remote_user = tk.StringVar(value="--")
        self.remote_fingerprint = tk.StringVar(value="--")
        self.forward_status = tk.StringVar(value="--")
        self.session_duration = tk.StringVar(value="--")
        self.command_summary = tk.StringVar(value="尚未解析命令")

        self._configure_style()
        self._build_layout()
        self._load_profiles()
        self._set_auth_mode()
        self._poll_events()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- styling and layout ----------
    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("App.TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure("Inner.TFrame", background=self.PANEL_2)
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT, font=("Segoe UI", 18, "bold"))
        style.configure("Section.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI", 10, "bold"))
        style.configure("Muted.TLabel", background=self.PANEL, foreground=self.MUTED, font=("Segoe UI", 9))
        style.configure("Body.TLabel", background=self.PANEL, foreground=self.TEXT, font=("Segoe UI", 9))
        style.configure("Status.TLabel", background=self.PANEL_2, foreground=self.MUTED, font=("Segoe UI", 9))
        style.configure("TEntry", fieldbackground="#0d151f", foreground=self.TEXT, insertcolor=self.TEXT, bordercolor=self.BORDER, lightcolor=self.BORDER, darkcolor=self.BORDER, padding=7)
        style.configure("TCombobox", fieldbackground="#0d151f", foreground=self.TEXT, selectbackground=self.BLUE, padding=5)
        style.configure("TButton", background="#1c2b3d", foreground=self.TEXT, bordercolor=self.BORDER, lightcolor="#1c2b3d", darkcolor="#1c2b3d", padding=(10, 7), font=("Segoe UI", 9))
        style.map("TButton", background=[("active", "#29415b"), ("disabled", "#16202c")], foreground=[("disabled", "#64758a")])
        style.configure("Primary.TButton", background=self.BLUE, foreground="#07111d", bordercolor=self.BLUE, padding=(13, 8), font=("Segoe UI", 9, "bold"))
        style.map("Primary.TButton", background=[("active", "#78baff"), ("disabled", "#375b7e")])
        style.configure("Danger.TButton", background="#542833", foreground="#ffd9de", bordercolor="#753844", padding=(10, 7))
        style.map("Danger.TButton", background=[("active", "#743846")])
        style.configure("TNotebook", background=self.PANEL, borderwidth=0)
        style.configure("TNotebook.Tab", background="#182535", foreground=self.MUTED, padding=(14, 8))
        style.map("TNotebook.Tab", background=[("selected", self.PANEL_2)], foreground=[("selected", self.TEXT)])
        style.configure("Treeview", background="#0d151f", fieldbackground="#0d151f", foreground=self.TEXT, bordercolor=self.BORDER, rowheight=27)
        style.configure("Treeview.Heading", background="#1a2a3c", foreground=self.MUTED, bordercolor=self.BORDER, font=("Segoe UI", 9, "bold"))
        style.map("Treeview", background=[("selected", "#234c73")], foreground=[("selected", "#ffffff")])

    def _build_layout(self) -> None:
        outer = ttk.Frame(self, style="App.TFrame", padding=18)
        outer.pack(fill="both", expand=True)

        top = ttk.Frame(outer, style="App.TFrame")
        top.pack(fill="x", pady=(0, 14))
        ttk.Label(top, text=APP_NAME, style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="LOCAL SSH WORKSPACE", style="Muted.TLabel").pack(side="left", padx=(13, 0), pady=(5, 0))
        self.state_badge = tk.Label(top, textvariable=self.connection_state, bg="#293342", fg=self.MUTED, padx=12, pady=5, font=("Segoe UI", 9, "bold"))
        self.state_badge.pack(side="right", pady=2)

        body = ttk.PanedWindow(outer, orient="horizontal")
        body.pack(fill="both", expand=True)

        self.sidebar = ttk.Frame(body, style="Panel.TFrame", padding=16)
        body.add(self.sidebar, weight=0)
        content = ttk.Frame(body, style="Panel.TFrame", padding=(16, 14, 16, 14))
        body.add(content, weight=1)

        self._build_sidebar()
        self._build_content(content)

    def _build_sidebar(self) -> None:
        ttk.Label(self.sidebar, text="连接配置", style="Section.TLabel").pack(anchor="w")
        ttk.Label(self.sidebar, text="选择一个配置，或创建新的连接。", style="Muted.TLabel").pack(anchor="w", pady=(4, 12))

        list_frame = ttk.Frame(self.sidebar, style="Inner.TFrame")
        list_frame.pack(fill="x", pady=(0, 12))
        self.profile_list = tk.Listbox(list_frame, height=7, activestyle="none", bg="#0d151f", fg=self.TEXT, selectbackground="#234c73", selectforeground="#ffffff", highlightthickness=0, relief="flat", font=("Segoe UI", 10))
        self.profile_list.pack(fill="both", expand=True, padx=1, pady=1)
        self.profile_list.bind("<<ListboxSelect>>", self._on_profile_select)

        profile_buttons = ttk.Frame(self.sidebar, style="Panel.TFrame")
        profile_buttons.pack(fill="x", pady=(0, 16))
        ttk.Button(profile_buttons, text="＋ 新建", command=self._new_profile).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(profile_buttons, text="删除", command=self._delete_profile).pack(side="left", fill="x", expand=True, padx=(4, 0))

        self._field(self.sidebar, "配置名称", self.profile_name)
        host_row = ttk.Frame(self.sidebar, style="Panel.TFrame")
        host_row.pack(fill="x", pady=(0, 9))
        host_col = ttk.Frame(host_row, style="Panel.TFrame")
        host_col.pack(side="left", fill="x", expand=True, padx=(0, 5))
        ttk.Label(host_col, text="主机", style="Muted.TLabel").pack(anchor="w", pady=(0, 3))
        ttk.Entry(host_col, textvariable=self.host).pack(fill="x")
        port_col = ttk.Frame(host_row, style="Panel.TFrame")
        port_col.pack(side="right", fill="x", padx=(5, 0))
        ttk.Label(port_col, text="端口", style="Muted.TLabel").pack(anchor="w", pady=(0, 3))
        self.port_entry = ttk.Entry(port_col, textvariable=self.port, width=7)
        self.port_entry.pack(fill="x")
        self._field(self.sidebar, "用户名", self.username)

        ttk.Label(self.sidebar, text="认证方式", style="Muted.TLabel").pack(anchor="w", pady=(1, 4))
        auth_row = ttk.Frame(self.sidebar, style="Panel.TFrame")
        auth_row.pack(fill="x", pady=(0, 9))
        ttk.Radiobutton(auth_row, text="密码", variable=self.auth_mode, value="password", command=self._set_auth_mode).pack(side="left")
        ttk.Radiobutton(auth_row, text="私钥", variable=self.auth_mode, value="key", command=self._set_auth_mode).pack(side="left", padx=(15, 0))

        self.auth_fields = ttk.Frame(self.sidebar, style="Panel.TFrame")
        self.auth_fields.pack(fill="x")

        self.password_frame = ttk.Frame(self.auth_fields, style="Panel.TFrame")
        self.password_frame.pack(fill="x")
        ttk.Label(self.password_frame, text="密码（仅当前会话）", style="Muted.TLabel").pack(anchor="w", pady=(0, 3))
        ttk.Entry(self.password_frame, textvariable=self.password, show="•").pack(fill="x", pady=(0, 9))

        self.key_frame = ttk.Frame(self.auth_fields, style="Panel.TFrame")
        ttk.Label(self.key_frame, text="私钥文件", style="Muted.TLabel").pack(anchor="w", pady=(0, 3))
        key_row = ttk.Frame(self.key_frame, style="Panel.TFrame")
        key_row.pack(fill="x", pady=(0, 7))
        ttk.Entry(key_row, textvariable=self.key_path).pack(side="left", fill="x", expand=True)
        ttk.Button(key_row, text="浏览", command=self._browse_key).pack(side="right", padx=(5, 0))
        ttk.Label(self.key_frame, text="私钥口令（可选，仅当前会话）", style="Muted.TLabel").pack(anchor="w", pady=(0, 3))
        ttk.Entry(self.key_frame, textvariable=self.key_passphrase, show="•").pack(fill="x", pady=(0, 9))

        ttk.Button(self.sidebar, text="保存配置", command=self._save_current_profile).pack(fill="x", pady=(2, 8))
        self.connect_button = ttk.Button(self.sidebar, text="连接 SSH", style="Primary.TButton", command=self._toggle_connection)
        self.connect_button.pack(fill="x")

        hint = tk.Label(self.sidebar, text="密码和私钥口令不会写入磁盘。\n首次连接未知主机时会显示指纹。", justify="left", anchor="w", bg=self.PANEL, fg=self.MUTED, font=("Segoe UI", 8), wraplength=235)
        hint.pack(fill="x", pady=(18, 0))

    def _build_content(self, parent: ttk.Frame) -> None:
        command_panel = tk.Frame(parent, bg=self.PANEL_2, highlightbackground=self.BORDER, highlightthickness=1)
        command_panel.pack(fill="x", pady=(0, 12))
        command_top = tk.Frame(command_panel, bg=self.PANEL_2)
        command_top.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(command_top, text="SSH 命令解析 / 执行", bg=self.PANEL_2, fg=self.TEXT, font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Label(command_top, text="支持 -D / -N / -p / -l / -i / -o ServerAliveInterval", bg=self.PANEL_2, fg=self.MUTED, font=("Segoe UI", 8)).pack(side="left", padx=(12, 0))
        command_row = ttk.Frame(command_panel, style="Inner.TFrame")
        command_row.pack(fill="x", padx=12, pady=(0, 5))
        self.ssh_command_entry = ttk.Entry(command_row)
        self.ssh_command_entry.pack(side="left", fill="x", expand=True, padx=(1, 7), pady=1)
        self.ssh_command_entry.insert(0, "ssh -N -D 1081 -o ServerAliveInterval=60 root@47.88.77.200")
        self.parse_button = ttk.Button(command_row, text="解析", command=self._parse_command)
        self.parse_button.pack(side="right", padx=(4, 0))
        self.execute_button = ttk.Button(command_row, text="执行", style="Primary.TButton", command=self._execute_command_action)
        self.execute_button.pack(side="right", padx=(4, 0))
        ttk.Label(command_panel, textvariable=self.command_summary, style="Muted.TLabel", wraplength=780, justify="left").pack(anchor="w", padx=13, pady=(0, 10))

        header = ttk.Frame(parent, style="Panel.TFrame")
        header.pack(fill="x", pady=(0, 13))
        heading = ttk.Frame(header, style="Panel.TFrame")
        heading.pack(side="left", fill="x", expand=True)
        ttk.Label(heading, text="终端会话", style="Section.TLabel").pack(anchor="w")
        ttk.Label(heading, textvariable=self.connection_detail, style="Muted.TLabel").pack(anchor="w", pady=(3, 0))
        ttk.Button(header, text="清空终端", command=self._clear_terminal).pack(side="right", padx=(8, 0))
        self.interrupt_button = ttk.Button(header, text="发送 Ctrl+C", command=self._send_interrupt, state="disabled")
        self.interrupt_button.pack(side="right")

        main_split = ttk.PanedWindow(parent, orient="horizontal")
        main_split.pack(fill="both", expand=True)
        terminal_panel = ttk.Frame(main_split, style="Panel.TFrame")
        main_split.add(terminal_panel, weight=1)
        info_panel = ttk.Frame(main_split, style="Panel.TFrame", width=255)
        main_split.add(info_panel, weight=0)

        terminal_box = tk.Frame(terminal_panel, bg=self.TERMINAL_BG, highlightbackground=self.BORDER, highlightthickness=1)
        terminal_box.pack(fill="both", expand=True)
        self.terminal = ScrolledText(terminal_box, wrap="word", state="disabled", bg=self.TERMINAL_BG, fg="#d8e4f2", insertbackground="#ffffff", selectbackground="#264a70", relief="flat", borderwidth=0, padx=14, pady=13, font=("Cascadia Mono", 10), undo=False)
        self.terminal.pack(fill="both", expand=True)
        self.terminal.tag_configure("output", foreground="#d8e4f2")
        self.terminal.tag_configure("system", foreground=self.AMBER)
        self.terminal.tag_configure("error", foreground="#ff8793")
        self.terminal.tag_configure("command", foreground="#75bcff")

        quick = ttk.Frame(terminal_panel, style="Panel.TFrame")
        quick.pack(fill="x", pady=(10, 8))
        ttk.Label(quick, text="快捷命令", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        for label, command in (("pwd", "pwd"), ("ls -la", "ls -la"), ("whoami", "whoami"), ("uname -a", "uname -a"), ("df -h", "df -h")):
            ttk.Button(quick, text=label, command=lambda value=command: self._send_command(value)).pack(side="left", padx=3)

        input_row = ttk.Frame(terminal_panel, style="Panel.TFrame")
        input_row.pack(fill="x")
        self.command_entry = ttk.Entry(input_row)
        self.command_entry.pack(side="left", fill="x", expand=True)
        self.command_entry.bind("<Return>", self._on_command_return)
        self.command_entry.bind("<Up>", self._history_up)
        self.command_entry.bind("<Down>", self._history_down)
        self.command_entry.bind("<Control-c>", self._on_entry_ctrl_c)
        self.send_button = ttk.Button(input_row, text="发送", style="Primary.TButton", command=self._send_from_entry, state="disabled")
        self.send_button.pack(side="right", padx=(8, 0))

        self._build_info_panel(info_panel)

    def _build_info_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="会话信息", style="Section.TLabel").pack(anchor="w", pady=(0, 10))
        card = tk.Frame(parent, bg=self.PANEL_2, highlightbackground=self.BORDER, highlightthickness=1)
        card.pack(fill="x")
        rows = (("远端主机", self.remote_host), ("登录用户", self.remote_user), ("本地转发", self.forward_status), ("连接时长", self.session_duration), ("Host key", self.remote_fingerprint))
        for index, (label, variable) in enumerate(rows):
            row = tk.Frame(card, bg=self.PANEL_2)
            row.pack(fill="x", padx=12, pady=(11 if index == 0 else 7, 0))
            tk.Label(row, text=label, bg=self.PANEL_2, fg=self.MUTED, font=("Segoe UI", 8)).pack(anchor="w")
            tk.Label(row, textvariable=variable, bg=self.PANEL_2, fg=self.TEXT, font=("Segoe UI", 9), wraplength=205, justify="left", anchor="w").pack(anchor="w", pady=(2, 0))
        tk.Frame(card, bg=self.PANEL_2, height=11).pack()

        ttk.Label(parent, text="安全提示", style="Section.TLabel").pack(anchor="w", pady=(22, 9))
        note = tk.Label(parent, text="• 使用系统 known_hosts 作为已知主机来源\n\n• 未知 Host key 会在连接后显示 SHA-256 指纹\n\n• 连接配置可保存，但密码不会落盘", bg=self.PANEL, fg=self.MUTED, justify="left", anchor="nw", wraplength=225, font=("Segoe UI", 9))
        note.pack(fill="x")

        ttk.Label(parent, text="连接日志", style="Section.TLabel").pack(anchor="w", pady=(22, 9))
        self.activity = ScrolledText(parent, height=8, state="disabled", wrap="word", bg="#0d151f", fg=self.MUTED, relief="flat", borderwidth=0, padx=9, pady=8, font=("Cascadia Mono", 8))
        self.activity.pack(fill="both", expand=True)

    def _field(self, parent: ttk.Frame, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label, style="Muted.TLabel").pack(anchor="w", pady=(0, 3))
        ttk.Entry(parent, textvariable=variable).pack(fill="x", pady=(0, 9))

    # ---------- profiles ----------
    def _load_profiles(self) -> None:
        try:
            if PROFILE_FILE.exists():
                data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self.profiles = [item for item in data if isinstance(item, dict)]
        except (OSError, json.JSONDecodeError) as exc:
            self._log_activity(f"配置读取失败: {exc}")
        self._refresh_profile_list()
        if self.profiles:
            self.profile_list.selection_set(0)
            self._load_profile(0)

    def _refresh_profile_list(self) -> None:
        self.profile_list.delete(0, tk.END)
        for item in self.profiles:
            name = str(item.get("name") or item.get("host") or "未命名配置")
            host = str(item.get("host") or "")
            self.profile_list.insert(tk.END, f"{name}   ·   {host}" if host else name)

    def _on_profile_select(self, _event: tk.Event | None = None) -> None:
        selected = self.profile_list.curselection()
        if selected:
            self._load_profile(selected[0])

    def _load_profile(self, index: int) -> None:
        if index < 0 or index >= len(self.profiles):
            return
        item = self.profiles[index]
        self.selected_profile_index = index
        self.profile_name.set(str(item.get("name", "")))
        self.host.set(str(item.get("host", "")))
        self.port.set(str(item.get("port", 22)))
        self.username.set(str(item.get("username", "")))
        mode = str(item.get("auth", "password"))
        self.auth_mode.set(mode if mode in ("password", "key") else "password")
        self.key_path.set(str(item.get("key_path", "")))
        self.password.set("")
        self.key_passphrase.set("")
        self._set_auth_mode()

    def _new_profile(self) -> None:
        if self.client:
            self._append_terminal("\n请先断开当前连接。\n", "system")
            return
        self.selected_profile_index = None
        self.profile_list.selection_clear(0, tk.END)
        self.profile_name.set("新连接")
        self.host.set("")
        self.port.set("22")
        self.username.set("")
        self.auth_mode.set("password")
        self.password.set("")
        self.key_path.set("")
        self.key_passphrase.set("")
        self._set_auth_mode()
        self.host.focus_set()

    def _delete_profile(self) -> None:
        selected = self.profile_list.curselection()
        if not selected:
            return
        index = selected[0]
        name = self.profiles[index].get("name", "此配置")
        if not messagebox.askyesno("删除配置", f"确定删除“{name}”？"):
            return
        self.profiles.pop(index)
        self._write_profiles()
        self._refresh_profile_list()
        if self.profiles:
            self.profile_list.selection_set(min(index, len(self.profiles) - 1))
            self._load_profile(min(index, len(self.profiles) - 1))
        else:
            self._new_profile()

    def _profile_from_form(self) -> dict[str, Any]:
        try:
            port = int(self.port.get().strip() or "22")
        except ValueError as exc:
            raise ValueError("端口必须是数字") from exc
        if not 1 <= port <= 65535:
            raise ValueError("端口范围应为 1-65535")
        host = self.host.get().strip()
        username = self.username.get().strip()
        if not host:
            raise ValueError("请填写主机地址")
        if not username:
            raise ValueError("请填写用户名")
        return {"name": self.profile_name.get().strip() or host, "host": host, "port": port, "username": username, "auth": self.auth_mode.get(), "key_path": self.key_path.get().strip()}

    def _save_current_profile(self) -> bool:
        try:
            profile = self._profile_from_form()
        except ValueError as exc:
            messagebox.showerror("配置不完整", str(exc))
            return False
        if self.selected_profile_index is None:
            self.profiles.append(profile)
            self.selected_profile_index = len(self.profiles) - 1
        else:
            self.profiles[self.selected_profile_index] = profile
        self._write_profiles()
        self._refresh_profile_list()
        self.profile_list.selection_set(self.selected_profile_index)
        self._log_activity(f"已保存配置: {profile['name']}")
        return True

    def _write_profiles(self) -> None:
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            PROFILE_FILE.write_text(json.dumps(self.profiles, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("保存失败", f"无法写入配置文件：\n{exc}")

    # ---------- connection ----------
    def _toggle_connection(self) -> None:
        if self.client or self.worker:
            self._disconnect()
        else:
            self._connect()

    def _parse_command(self) -> SSHCommand | None:
        try:
            parsed = parse_ssh_command(self.ssh_command_entry.get())
        except SSHCommandError as exc:
            self.parsed_ssh_command = None
            self.command_summary.set(f"解析失败：{exc}")
            self._log_activity(f"命令解析失败: {exc}")
            return None
        self.parsed_ssh_command = parsed
        self.selected_profile_index = None
        self.profile_list.selection_clear(0, tk.END)
        self.profile_name.set(parsed.destination)
        self.host.set(parsed.host)
        self.port.set(str(parsed.port))
        self.username.set(parsed.username)
        if parsed.identity_file:
            self.key_path.set(parsed.identity_file)
            self.auth_mode.set("key")
        else:
            # A pasted command without -i follows SSH's default agent/key
            # lookup; do not accidentally reuse a key selected for an older
            # profile.
            self.key_path.set("")
            self.key_passphrase.set("")
            self.auth_mode.set("password")
        self._set_auth_mode()
        self.command_summary.set(parsed.summary)
        self._log_activity(f"命令解析成功: {parsed.summary}")
        return parsed

    def _execute_parsed_command(self) -> None:
        if self.client or self.worker:
            self._append_terminal("\n请先断开当前 SSH 会话。\n", "system")
            return
        parsed = self._parse_command()
        if parsed is not None:
            self.active_ssh_command = parsed
            self._connect(parsed)

    def _execute_command_action(self) -> None:
        if self.client or self.worker:
            if self.active_ssh_command is not None:
                self._disconnect()
            else:
                self._append_terminal("\n当前是普通 SSH 会话，请使用左侧“连接 SSH”按钮断开。\n", "system")
            return
        self._execute_parsed_command()

    def _connect(self, parsed_command: SSHCommand | None = None) -> None:
        if parsed_command is None:
            self.active_ssh_command = None
        try:
            profile = self._profile_from_form()
            port = int(profile["port"])
        except ValueError as exc:
            messagebox.showerror("无法连接", str(exc))
            return
        if self.auth_mode.get() == "key" and not self.key_path.get().strip():
            messagebox.showerror("无法连接", "已选择私钥认证，请选择私钥文件")
            return
        if parsed_command is None:
            self._save_current_profile()
        self.stop_event.clear()
        self.connection_state.set("连接中")
        if parsed_command is None:
            detail = f"正在连接 {profile['username']}@{profile['host']}:{port} …"
        else:
            detail = f"正在执行 · {parsed_command.summary}"
        self.connection_detail.set(detail)
        self._set_connection_controls(working=True)
        self._append_terminal(f"连接到 {profile['username']}@{profile['host']}:{port} …\n", "system")
        self._log_activity(f"开始连接 {profile['host']}:{port}")
        credentials = {
            "password": self.password.get(),
            "key_passphrase": self.key_passphrase.get(),
        }
        self.worker = threading.Thread(target=self._connection_worker, args=(profile, credentials, parsed_command), daemon=True)
        self.worker.start()

    def _connection_worker(
        self,
        profile: dict[str, Any],
        credentials: dict[str, str],
        parsed_command: SSHCommand | None = None,
    ) -> None:
        client = paramiko.SSHClient()
        policy = RememberingHostKeyPolicy()
        executor: SSHCommandExecutor | None = None
        try:
            try:
                client.load_system_host_keys()
            except OSError:
                pass
            client.set_missing_host_key_policy(policy)
            kwargs: dict[str, Any] = {
                "hostname": profile["host"],
                "port": int(profile["port"]),
                "username": profile["username"],
                "timeout": parsed_command.connect_timeout if parsed_command else 10,
                "banner_timeout": parsed_command.connect_timeout if parsed_command else 10,
                "auth_timeout": parsed_command.connect_timeout if parsed_command else 10,
                "look_for_keys": False,
                "allow_agent": False,
            }
            if profile["auth"] == "key":
                kwargs["key_filename"] = profile["key_path"]
                passphrase = credentials["key_passphrase"]
                if passphrase:
                    kwargs["password"] = passphrase
            else:
                if credentials["password"]:
                    kwargs["password"] = credentials["password"]
                else:
                    kwargs["look_for_keys"] = True
                    kwargs["allow_agent"] = True
            client.connect(**kwargs)
            transport = client.get_transport()
            if transport is None:
                raise RuntimeError("SSH transport 未建立")
            if parsed_command is not None and parsed_command.server_alive_interval:
                transport.set_keepalive(parsed_command.server_alive_interval)
                self.events.put(("activity", f"SSH 保活已设置为每 {parsed_command.server_alive_interval} 秒"))
            if parsed_command is not None:
                executor = SSHCommandExecutor(
                    client,
                    transport,
                    lambda text: self.events.put(("activity", text)),
                )
                channel = executor.start(parsed_command)
            else:
                channel = client.invoke_shell(term="xterm-256color", width=120, height=38)
                channel.settimeout(0.25)
            key = transport.get_remote_server_key()
            digest = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")
            fingerprint = policy.fingerprint or digest
            if self.stop_event.is_set():
                if channel is not None:
                    channel.close()
                client.close()
                return
            with self.io_lock:
                self.client = client
                self.channel = channel
                self.command_executor = executor
            self.events.put(("connected", {"fingerprint": fingerprint, "server_key": key.get_name(), "command": parsed_command}))
            while not self.stop_event.is_set():
                try:
                    if channel is not None and channel.recv_ready():
                        data = channel.recv(65536)
                        if not data:
                            break
                        self.events.put(("output", data.decode("utf-8", errors="replace")))
                    elif channel is not None and channel.exit_status_ready():
                        break
                    elif channel is None and not transport.is_active():
                        break
                    else:
                        time.sleep(0.05)
                except socket.timeout:
                    continue
                except (OSError, paramiko.SSHException) as exc:
                    if not self.stop_event.is_set():
                        self.events.put(("error", str(exc)))
                    break
        except Exception as exc:  # Paramiko exposes several auth/key exceptions.
            self.events.put(("connect_error", str(exc)))
        finally:
            if executor is not None:
                executor.stop()
            try:
                client.close()
            except Exception:
                pass
            self.events.put(("worker_done", None))

    def _disconnect(self) -> None:
        self.stop_event.set()
        with self.io_lock:
            channel, client, executor = self.channel, self.client, self.command_executor
            self.channel = None
            self.client = None
            self.command_executor = None
        if executor is not None:
            executor.stop()
        try:
            if channel:
                channel.close()
        except Exception:
            pass
        try:
            if client:
                client.close()
        except Exception:
            pass
        self.worker = None
        self.active_ssh_command = None
        self.connected_at = None
        self.connection_state.set("未连接")
        self.connection_detail.set("连接已关闭")
        self.remote_host.set("--")
        self.remote_user.set("--")
        self.forward_status.set("--")
        self.remote_fingerprint.set("--")
        self.session_duration.set("--")
        self._set_connection_controls(working=False)
        self._append_terminal("\n[连接已关闭]\n", "system")
        self._log_activity("连接已关闭")

    def _set_connection_controls(self, working: bool) -> None:
        if working:
            self.connect_button.configure(text="断开连接", style="Danger.TButton")
            self.send_button.configure(state="disabled")
            self.interrupt_button.configure(state="disabled")
            self.state_badge.configure(bg="#5b4a2a", fg=self.AMBER)
            if self.active_ssh_command is not None:
                self.execute_button.configure(text="停止命令", style="Danger.TButton")
        else:
            connected = self.client is not None
            self.connect_button.configure(
                text="断开连接" if connected else "连接 SSH",
                style="Danger.TButton" if connected else "Primary.TButton",
            )
            state = "normal" if connected else "disabled"
            self.send_button.configure(state=state)
            self.interrupt_button.configure(state=state)
            self.state_badge.configure(
                bg="#193d31" if connected else "#293342",
                fg=self.GREEN if connected else self.MUTED,
            )
            if connected and self.active_ssh_command is not None:
                self.execute_button.configure(text="停止命令", style="Danger.TButton")
            else:
                self.execute_button.configure(text="执行", style="Primary.TButton")

    # ---------- terminal and event loop ----------
    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "connected":
                    self.connected_at = time.monotonic()
                    self.current_fingerprint = payload["fingerprint"]
                    self.connection_state.set("已连接")
                    parsed_command = payload.get("command")
                    if isinstance(parsed_command, SSHCommand):
                        self.connection_detail.set(f"正在运行 · {parsed_command.summary}")
                    else:
                        self.connection_detail.set(f"{self.username.get()}@{self.host.get()} · 交互式 shell")
                    self.remote_host.set(self.host.get())
                    self.remote_user.set(self.username.get())
                    if isinstance(parsed_command, SSHCommand) and parsed_command.dynamic_forwards:
                        self.forward_status.set("\n".join(item.display for item in parsed_command.dynamic_forwards))
                    else:
                        self.forward_status.set("--")
                    self.remote_fingerprint.set(f"{payload['server_key']}\n{payload['fingerprint']}")
                    self._set_connection_controls(working=False)
                    if isinstance(parsed_command, SSHCommand) and parsed_command.no_remote_command:
                        self.send_button.configure(state="disabled")
                        self.interrupt_button.configure(state="disabled")
                    if isinstance(parsed_command, SSHCommand) and parsed_command.no_remote_command:
                        self._append_terminal(f"SSH 转发已就绪：{parsed_command.summary}\n", "system")
                    elif isinstance(parsed_command, SSHCommand) and parsed_command.remote_command:
                        self._append_terminal(f"远端命令已启动：{parsed_command.remote_command}\n", "system")
                    else:
                        self._append_terminal("SSH shell 已就绪。\n", "system")
                    self._log_activity(f"连接成功，Host key: {payload['fingerprint']}")
                elif kind == "output":
                    self._append_terminal(self._clean_terminal_output(payload), "output")
                elif kind == "activity":
                    self._log_activity(str(payload))
                elif kind == "error":
                    self._append_terminal(f"\n远端 I/O 错误: {payload}\n", "error")
                    self._log_activity(f"I/O 错误: {payload}")
                elif kind == "connect_error":
                    self.connection_state.set("连接失败")
                    self.connection_detail.set("请检查主机、端口、凭据或网络")
                    self._append_terminal(f"连接失败: {payload}\n", "error")
                    self._log_activity(f"连接失败: {payload}")
                    self._set_connection_controls(working=False)
                elif kind == "worker_done":
                    if self.client and not self.stop_event.is_set():
                        self._disconnect()
                    self.worker = None
                    if not self.client:
                        self.command_summary.set("命令已停止")
        except queue.Empty:
            pass
        if self.connected_at and self.client:
            elapsed = int(time.monotonic() - self.connected_at)
            self.session_duration.set(self._format_duration(elapsed))
        self.after(80, self._poll_events)

    def _send_from_entry(self) -> None:
        command = self.command_entry.get()
        self.command_entry.delete(0, tk.END)
        self._send_command(command)

    def _on_command_return(self, _event: tk.Event) -> str:
        self._send_from_entry()
        return "break"

    def _send_command(self, command: str) -> None:
        if not self.client or not self.channel:
            self._append_terminal("\n尚未连接 SSH。\n", "system")
            return
        if command is None:
            return
        command = str(command)
        if command:
            self.command_history.append(command)
            self.command_history = self.command_history[-100:]
            self.history_index = len(self.command_history)
        self._send_raw(command + "\n")

    def _send_raw(self, text: str) -> None:
        with self.io_lock:
            channel = self.channel
            if not channel:
                return
            try:
                channel.sendall(text)
            except (OSError, paramiko.SSHException) as exc:
                self._append_terminal(f"发送失败: {exc}\n", "error")

    def _send_interrupt(self) -> None:
        self._send_raw("\x03")

    def _on_entry_ctrl_c(self, _event: tk.Event) -> str:
        self._send_interrupt()
        return "break"

    def _history_up(self, _event: tk.Event) -> str:
        if self.command_history:
            self.history_index = max(0, self.history_index - 1)
            self.command_entry.delete(0, tk.END)
            self.command_entry.insert(0, self.command_history[self.history_index])
        return "break"

    def _history_down(self, _event: tk.Event) -> str:
        if self.command_history:
            self.history_index = min(len(self.command_history), self.history_index + 1)
            self.command_entry.delete(0, tk.END)
            if self.history_index < len(self.command_history):
                self.command_entry.insert(0, self.command_history[self.history_index])
        return "break"

    def _append_terminal(self, text: str, tag: str = "output") -> None:
        self.terminal.configure(state="normal")
        self.terminal.insert(tk.END, text, tag)
        self.terminal.see(tk.END)
        self.terminal.configure(state="disabled")

    @staticmethod
    def _clean_terminal_output(text: str) -> str:
        """Remove terminal control sequences that Tk Text cannot render."""
        text = ANSI_ESCAPE.sub("", text)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\x08", "")
        return "".join(char for char in text if char in ("\n", "\t") or ord(char) >= 32)

    def _clear_terminal(self) -> None:
        self.terminal.configure(state="normal")
        self.terminal.delete("1.0", tk.END)
        self.terminal.configure(state="disabled")

    def _log_activity(self, text: str) -> None:
        if not hasattr(self, "activity"):
            return
        stamp = time.strftime("%H:%M:%S")
        self.activity.configure(state="normal")
        self.activity.insert(tk.END, f"{stamp}  {text}\n")
        self.activity.see(tk.END)
        self.activity.configure(state="disabled")

    # ---------- small UI helpers ----------
    def _set_auth_mode(self) -> None:
        if not hasattr(self, "password_frame"):
            return
        if self.auth_mode.get() == "key":
            self.password_frame.pack_forget()
            self.key_frame.pack(fill="x")
        else:
            self.key_frame.pack_forget()
            self.password_frame.pack(fill="x")

    def _browse_key(self) -> None:
        path = filedialog.askopenfilename(title="选择 SSH 私钥", filetypes=[("SSH key", "*.pem *.key id_rsa id_ed25519 *.*"), ("All files", "*.*")])
        if path:
            self.key_path.set(path)

    @staticmethod
    def _format_duration(seconds: int) -> str:
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _on_close(self) -> None:
        if self.client or self.worker:
            self._disconnect()
        self.destroy()


if __name__ == "__main__":
    app = NexusSSH()
    app.mainloop()
