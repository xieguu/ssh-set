"""Codex Config Studio - one-file local visual config editor and launcher.

This is intentionally a personal desktop utility. It edits the user Codex
config in place, keeps provider bearer tokens visible, and starts codex.cmd
with explicit provider/model/reasoning overrides so the selected values win.
"""

from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
import tomllib
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any
from urllib import error as url_error
from urllib import request as url_request


APP_TITLE = "Codex Config Studio"
CODEX_HOME = Path.home() / ".codex"
CONFIG_PATH = CODEX_HOME / "config.toml"
CODEX_COMMAND = "codex.cmd"
API_PROTOCOL_OPENAI = "openai"
API_PROTOCOL_ANTHROPIC = "anthropic"
API_PROTOCOLS = (API_PROTOCOL_OPENAI, API_PROTOCOL_ANTHROPIC)
TEST_TIMEOUT_SECONDS = 20


def toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def toml_key(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    return toml_string(value)


def load_config() -> dict[str, Any]:
    try:
        return tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}


def dict_value(data: dict[str, Any], key: str, default: Any = "") -> Any:
    value = data.get(key, default)
    return default if value is None else value


def normalize_api_protocol(value: str) -> str:
    return value if value in API_PROTOCOLS else API_PROTOCOL_OPENAI


def api_protocol_label(value: str) -> str:
    return "Anthropic" if normalize_api_protocol(value) == API_PROTOCOL_ANTHROPIC else "OpenAI"


def test_endpoint(base_url: str, api_protocol: str) -> str:
    """Build a standard health-check endpoint from a provider base URL."""
    base = base_url.strip().rstrip("/")
    if not base:
        raise ValueError("Base URL 不能为空")
    protocol = normalize_api_protocol(api_protocol)
    suffix = "/messages" if protocol == API_PROTOCOL_ANTHROPIC else "/responses"
    if base.lower().endswith(suffix):
        return base
    if base.lower().endswith("/v1"):
        return base + suffix
    return base + "/v1" + suffix


def test_request_payload(provider: "Provider", model: str) -> tuple[str, dict[str, str], bytes]:
    """Create a minimal request without exposing the bearer token in logs."""
    protocol = normalize_api_protocol(provider.api_protocol)
    endpoint = test_endpoint(provider.base_url, protocol)
    if not model.strip():
        raise ValueError("模型 ID 不能为空")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "codex-config-studio/1.0",
    }
    if provider.token:
        if protocol == API_PROTOCOL_ANTHROPIC:
            headers["x-api-key"] = provider.token
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {provider.token}"
    if protocol == API_PROTOCOL_ANTHROPIC:
        payload = {
            "model": model.strip(),
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "只回复 OK"}],
        }
    else:
        payload = {
            "model": model.strip(),
            "input": "只回复 OK",
            "max_output_tokens": 16,
        }
    return endpoint, headers, json.dumps(payload, ensure_ascii=False).encode("utf-8")


def redact_secret(value: str, secret: str) -> str:
    if not secret:
        return value
    return value.replace(secret, "***")


def response_summary(api_protocol: str, raw_text: str) -> str:
    """Extract a short response preview suitable for the GUI log."""
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return raw_text.strip()[:240] or "（空响应）"
    protocol = normalize_api_protocol(api_protocol)
    if protocol == API_PROTOCOL_ANTHROPIC:
        content = payload.get("content") if isinstance(payload, dict) else None
        if isinstance(content, list):
            texts = [item.get("text", "") for item in content if isinstance(item, dict)]
            if any(texts):
                return " ".join(texts).strip()[:240]
    output_text = payload.get("output_text") if isinstance(payload, dict) else None
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()[:240]
    output = payload.get("output") if isinstance(payload, dict) else None
    if isinstance(output, list):
        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []):
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    texts.append(content["text"])
        if texts:
            return " ".join(texts).strip()[:240]
    if isinstance(payload, dict):
        error_payload = payload.get("error")
        if error_payload:
            return json.dumps(error_payload, ensure_ascii=False)[:240]
        for key in ("message", "id", "type"):
            if isinstance(payload.get(key), str):
                return payload[key][:240]
    return json.dumps(payload, ensure_ascii=False)[:240]


def provider_toml(provider: "Provider") -> str:
    api_protocol = normalize_api_protocol(provider.api_protocol)
    lines = [f"[model_providers.{toml_key(provider.provider_id)}]"]
    # This is a comment because Codex 0.148.0's official config schema only
    # accepts wire_api = "responses". It lets this GUI remember the user's
    # OpenAI/Anthropic family choice without making config.toml invalid.
    lines.append(f"# codex_config_studio_api_protocol = {toml_string(api_protocol)}")
    lines.append(f"name = {toml_string(provider.name)}")
    if provider.base_url:
        lines.append(f"base_url = {toml_string(provider.base_url)}")
    lines.append('wire_api = "responses"')
    lines.append(f"requires_openai_auth = {str(provider.requires_openai_auth).lower()}")
    if provider.supports_websockets:
        lines.append("supports_websockets = true")
    if provider.token:
        lines.append(f"experimental_bearer_token = {toml_string(provider.token)}")
    return "\n".join(lines)


@dataclass
class Provider:
    provider_id: str
    name: str = ""
    base_url: str = ""
    wire_api: str = "responses"
    token: str = ""
    requires_openai_auth: bool = False
    supports_websockets: bool = False
    api_protocol: str = API_PROTOCOL_OPENAI

    @classmethod
    def from_dict(
        cls,
        provider_id: str,
        data: dict[str, Any],
        api_protocol: str = API_PROTOCOL_OPENAI,
    ) -> "Provider":
        return cls(
            provider_id=provider_id,
            name=str(dict_value(data, "name", provider_id)),
            base_url=str(dict_value(data, "base_url", "")),
            wire_api=str(dict_value(data, "wire_api", "responses")),
            token=str(dict_value(data, "experimental_bearer_token", "")),
            requires_openai_auth=bool(dict_value(data, "requires_openai_auth", False)),
            supports_websockets=bool(dict_value(data, "supports_websockets", False)),
            api_protocol=normalize_api_protocol(api_protocol),
        )


SECTION_HEADER = re.compile(r"(?m)^[ \t]*\[([^\]\r\n]+)\][ \t]*(?:#.*)?$")
PROVIDER_HEADER = re.compile(r'''^model_providers\.(?:"([^"]+)"|'([^']+)'|([A-Za-z0-9_-]+))(.*)$''')
PROTOCOL_COMMENT = re.compile(
    r'''^[ \t]*#[ \t]*codex_config_studio_api_protocol[ \t]*=[ \t]*["']?(openai|anthropic)["']?[ \t]*$''',
    re.IGNORECASE,
)
MANAGED_PROVIDER_KEYS = {
    "name",
    "base_url",
    "wire_api",
    "requires_openai_auth",
    "supports_websockets",
    "experimental_bearer_token",
}


def split_toml_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    matches = list(SECTION_HEADER.finditer(text))
    if not matches:
        return text, []
    prefix = text[: matches[0].start()]
    blocks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks.append((match.group(1).strip(), text[match.start() : end]))
    return prefix, blocks


def provider_header_info(header: str) -> tuple[str, bool] | None:
    match = PROVIDER_HEADER.fullmatch(header.strip())
    if not match:
        return None
    provider_id = next(value for value in match.groups()[:3] if value is not None)
    suffix = match.group(4).strip()
    return provider_id, suffix == ""


def provider_protocols_from_text(text: str) -> dict[str, str]:
    """Read this app's harmless comment metadata from provider sections."""
    _prefix, blocks = split_toml_sections(text)
    protocols: dict[str, str] = {}
    for header, block in blocks:
        info = provider_header_info(header)
        if info is None or not info[1]:
            continue
        match = next(
            (candidate for line in block.splitlines() if (candidate := PROTOCOL_COMMENT.fullmatch(line))),
            None,
        )
        if match:
            protocols[info[0]] = normalize_api_protocol(match.group(1).lower())
    return protocols


def update_root_keys(prefix: str, updates: dict[str, str]) -> str:
    keys = set(updates)
    kept: list[str] = []
    for line in prefix.splitlines():
        match = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=", line)
        if match and match.group(1) in keys:
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    managed = [f"{key} = {toml_string(value)}" for key, value in updates.items()]
    tail = "\n".join(kept).rstrip()
    return "\n".join(managed) + ("\n" + tail if tail else "") + "\n\n"


def update_provider_block(block: str, provider: Provider) -> str:
    lines = block.splitlines()
    # split_toml_sections() normally starts each block at its header. Keep this
    # defensive lookup so leading blank lines can never be emitted as a second
    # copy of the provider header if the splitter changes later.
    header_index = next(
        (index for index, line in enumerate(lines) if SECTION_HEADER.fullmatch(line)),
        0,
    )
    preserved: list[str] = []
    for line in lines[header_index + 1 :]:
        if PROTOCOL_COMMENT.fullmatch(line):
            continue
        match = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=", line)
        if match and match.group(1) in MANAGED_PROVIDER_KEYS:
            continue
        preserved.append(line)
    while preserved and not preserved[-1].strip():
        preserved.pop()
    generated = provider_toml(provider).splitlines()
    if preserved:
        generated.extend(preserved)
    return "\n".join(generated).rstrip() + "\n\n"


def merge_config_text(
    original: str,
    providers: list[Provider],
    active_provider: str,
    model: str,
    reasoning: str,
) -> str:
    """Update managed Codex values while preserving unrelated configuration."""
    prefix, blocks = split_toml_sections(original)
    provider_map = {item.provider_id: item for item in providers}
    emitted: set[str] = set()
    output = [
        update_root_keys(
            prefix,
            {
                "model_provider": active_provider,
                "model": model,
                "model_reasoning_effort": reasoning,
            },
        )
    ]
    for header, block in blocks:
        info = provider_header_info(header)
        if info is None:
            output.append(block.rstrip() + "\n\n")
            continue
        provider_id, is_top_level = info
        provider = provider_map.get(provider_id)
        if provider is None:
            # Provider was deleted or renamed; its top-level and nested blocks
            # are deliberately removed.
            continue
        if is_top_level:
            if provider_id in emitted:
                # Tolerate a hand-edited/legacy file with duplicate provider
                # tables and emit one canonical definition.
                continue
            output.append(update_provider_block(block, provider))
            emitted.add(provider_id)
        else:
            # Preserve advanced nested provider sections such as auth tables.
            output.append(block.rstrip() + "\n\n")
    for provider in providers:
        if provider.provider_id not in emitted:
            output.append(provider_toml(provider) + "\n\n")
    return "".join(output).rstrip() + "\n"


class CodexConfigStudio(tk.Tk):
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
    TERMINAL = "#080c11"

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1450x900")
        self.minsize(1120, 720)
        self.configure(bg=self.BG)
        self.providers: list[Provider] = []
        self.selected_provider = 0
        self.config_data: dict[str, Any] = {}
        self.process: subprocess.Popen[str] | None = None
        self.test_events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.test_thread: threading.Thread | None = None

        self.provider_id = tk.StringVar()
        self.provider_name = tk.StringVar()
        self.base_url = tk.StringVar()
        self.api_protocol = tk.StringVar(value="OpenAI")
        self.protocol_hint = tk.StringVar()
        self.token = tk.StringVar()
        self.requires_openai_auth = tk.BooleanVar(value=False)
        self.supports_websockets = tk.BooleanVar(value=False)
        self.active_provider = tk.StringVar()
        self.active_model = tk.StringVar()
        self.reasoning_effort = tk.StringVar(value="high")
        self.prompt = tk.StringVar()
        self.sandbox = tk.StringVar(value="workspace-write")
        self.profile = tk.StringVar()
        self.extra_config = tk.StringVar()
        self.force_selection = tk.BooleanVar(value=True)
        self.config_status = tk.StringVar(value="未读取配置")
        self.command_status = tk.StringVar(value="等待生成命令")
        self.test_status = tk.StringVar(value="尚未测试")

        self._style()
        self._layout()
        self._load_config_into_ui()
        self._bind_reactive_inputs()
        self._refresh_command_preview()
        self.after(100, self._poll_process)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _style(self) -> None:
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
        style.configure("TEntry", fieldbackground="#0d151f", foreground=self.TEXT, insertcolor=self.TEXT, bordercolor=self.BORDER, lightcolor=self.BORDER, darkcolor=self.BORDER, padding=7)
        style.configure("TCombobox", fieldbackground="#0d151f", foreground=self.TEXT, selectbackground=self.BLUE, padding=6)
        style.configure("TRadiobutton", background=self.PANEL_2, foreground=self.TEXT)
        style.map("TRadiobutton", background=[("active", self.PANEL_2)], foreground=[("active", "#ffffff")])
        style.configure("TCheckbutton", background=self.PANEL, foreground=self.TEXT)
        style.map("TCheckbutton", background=[("active", self.PANEL)], foreground=[("active", "#ffffff")])
        style.configure("TButton", background="#1c2b3d", foreground=self.TEXT, bordercolor=self.BORDER, lightcolor="#1c2b3d", darkcolor="#1c2b3d", padding=(10, 7))
        style.map("TButton", background=[("active", "#29415b"), ("disabled", "#16202c")])
        style.configure("Primary.TButton", background=self.BLUE, foreground="#07111d", bordercolor=self.BLUE, padding=(13, 8), font=("Segoe UI", 9, "bold"))
        style.configure("Danger.TButton", background="#542833", foreground="#ffd9de", bordercolor="#753844", padding=(11, 8))
        style.configure("Treeview", background="#0d151f", fieldbackground="#0d151f", foreground=self.TEXT, rowheight=29, bordercolor=self.BORDER)
        style.configure("Treeview.Heading", background="#1a2a3c", foreground=self.MUTED, bordercolor=self.BORDER)
        style.map("Treeview", background=[("selected", "#234c73")], foreground=[("selected", "#ffffff")])

    def _layout(self) -> None:
        outer = ttk.Frame(self, style="App.TFrame", padding=18)
        outer.pack(fill="both", expand=True)
        top = ttk.Frame(outer, style="App.TFrame")
        top.pack(fill="x", pady=(0, 14))
        ttk.Label(top, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        ttk.Label(top, text="CONFIG → COMMAND → RUN", style="Muted.TLabel").pack(side="left", padx=(14, 0), pady=(5, 0))
        ttk.Button(top, text="重新读取 config.toml", command=self._reload_config).pack(side="right")

        split = ttk.PanedWindow(outer, orient="horizontal")
        split.pack(fill="both", expand=True)
        left = ttk.Frame(split, style="Panel.TFrame", padding=15)
        right = ttk.Frame(split, style="Panel.TFrame", padding=(15, 13, 15, 13))
        split.add(left, weight=0)
        split.add(right, weight=1)
        self._provider_panel(left)
        self._right_panel(right)

    def _provider_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="供应商配置", style="Section.TLabel").pack(anchor="w")
        ttk.Label(parent, text="添加第三方供应商，并配置它的 API 类型与连接信息。", style="Muted.TLabel", wraplength=285, justify="left").pack(anchor="w", pady=(4, 10))
        list_row = ttk.Frame(parent, style="Panel.TFrame")
        list_row.pack(fill="x", pady=(0, 9))
        self.provider_list = tk.Listbox(list_row, height=8, bg="#0d151f", fg=self.TEXT, selectbackground="#234c73", selectforeground="#ffffff", activestyle="none", highlightthickness=0, relief="flat", font=("Segoe UI", 10))
        self.provider_list.pack(fill="both", expand=True)
        self.provider_list.bind("<<ListboxSelect>>", self._select_provider)
        buttons = ttk.Frame(parent, style="Panel.TFrame")
        buttons.pack(fill="x", pady=(0, 14))
        ttk.Button(buttons, text="＋ 添加", command=self._add_provider).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ttk.Button(buttons, text="删除", command=self._delete_provider).pack(side="left", fill="x", expand=True, padx=(4, 0))

        self._entry(parent, "Provider ID", self.provider_id)
        self._entry(parent, "显示名称", self.provider_name)
        self._entry(parent, "Base URL", self.base_url)
        ttk.Label(parent, text="API 协议", style="Muted.TLabel").pack(anchor="w", pady=(1, 3))
        self.protocol_combo = ttk.Combobox(
            parent,
            textvariable=self.api_protocol,
            values=("OpenAI", "Anthropic"),
            state="readonly",
        )
        self.protocol_combo.pack(fill="x", pady=(0, 4))
        ttk.Label(
            parent,
            textvariable=self.protocol_hint,
            style="Muted.TLabel",
            wraplength=285,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))
        self._entry(parent, "Bearer Token（明文）", self.token)
        ttk.Checkbutton(parent, text="使用 Codex/OpenAI 登录认证（requires_openai_auth）", variable=self.requires_openai_auth).pack(anchor="w", pady=(2, 3))
        ttk.Checkbutton(parent, text="supports_websockets", variable=self.supports_websockets).pack(anchor="w", pady=(0, 10))
        provider_actions = ttk.Frame(parent, style="Panel.TFrame")
        provider_actions.pack(fill="x")
        ttk.Button(provider_actions, text="应用修改", command=self._apply_provider_form).pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.test_button = ttk.Button(provider_actions, text="连通测试", style="Primary.TButton", command=self._test_provider)
        self.test_button.pack(side="left", fill="x", expand=True, padx=(4, 0))
        ttk.Label(parent, textvariable=self.test_status, style="Muted.TLabel", wraplength=285, justify="left").pack(anchor="w", pady=(7, 0))
        ttk.Button(parent, text="写入 config.toml", style="Primary.TButton", command=self._write_config).pack(fill="x", pady=(8, 0))
        ttk.Label(parent, textvariable=self.config_status, style="Muted.TLabel", wraplength=260, justify="left").pack(anchor="w", pady=(12, 0))

    def _right_panel(self, parent: ttk.Frame) -> None:
        flow = tk.Frame(parent, bg=self.PANEL_2, highlightbackground=self.BORDER, highlightthickness=1)
        flow.pack(fill="x", pady=(0, 12))
        tk.Label(flow, text="执行流程", bg=self.PANEL_2, fg=self.TEXT, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=13, pady=(10, 3))
        flow_steps = tk.Frame(flow, bg=self.PANEL_2)
        flow_steps.pack(fill="x", padx=10, pady=(2, 11))
        steps = (
            ("01", "编辑供应商", "Provider\n字段"),
            ("02", "写入配置", "config.toml\n+ 自动备份"),
            ("03", "拼接命令", "固定块 +\n可变块"),
            ("04", "运行 Codex", "新终端\n交互执行"),
        )
        for index, (number, title, detail) in enumerate(steps):
            flow_steps.columnconfigure(index * 2, weight=1)
            card = tk.Frame(flow_steps, bg="#1b2b3c", highlightbackground=self.BORDER, highlightthickness=1)
            card.grid(row=0, column=index * 2, sticky="ew")
            tk.Label(card, text=number, bg="#244463", fg="#9bd0ff", font=("Cascadia Mono", 9, "bold")).pack(anchor="w", padx=8, pady=(7, 2))
            tk.Label(card, text=title, bg="#1b2b3c", fg=self.TEXT, font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=8)
            tk.Label(card, text=detail, bg="#1b2b3c", fg=self.MUTED, justify="left", font=("Segoe UI", 8)).pack(anchor="w", padx=8, pady=(1, 7))
            if index < len(steps) - 1:
                tk.Label(flow_steps, text="→", bg=self.PANEL_2, fg=self.BLUE, font=("Segoe UI", 14, "bold")).grid(row=0, column=index * 2 + 1, padx=6)

        settings = ttk.Frame(parent, style="Panel.TFrame")
        settings.pack(fill="x", pady=(0, 12))
        ttk.Label(settings, text="模型与命令参数", style="Section.TLabel").pack(anchor="w", pady=(0, 8))
        row = ttk.Frame(settings, style="Panel.TFrame")
        row.pack(fill="x")
        self._labeled_combo(row, "当前供应商", self.active_provider, (), 0, readonly=True)
        self._labeled_grid_entry(row, "模型 ID（任意自定义）", self.active_model, 1, weight=2)
        self._labeled_combo(row, "Reasoning", self.reasoning_effort, ("minimal", "low", "medium", "high", "xhigh"), 2)
        ttk.Label(settings, text="模型 ID 不做品牌限制，直接填写供应商提供的精确 ID。", style="Muted.TLabel").pack(anchor="w", pady=(4, 7))
        self._entry(settings, "Prompt（可选）", self.prompt)
        row2 = ttk.Frame(settings, style="Panel.TFrame")
        row2.pack(fill="x")
        self._labeled_combo(row2, "Sandbox", self.sandbox, ("read-only", "workspace-write", "danger-full-access"), 0)
        self._entry(row2, "Profile（可选）", self.profile, side=True)
        self._entry(settings, "额外 -c 参数（可选，逐项空格分隔）", self.extra_config)
        ttk.Checkbutton(settings, text="强制带上 provider / model / reasoning 参数", variable=self.force_selection, command=self._refresh_command_preview).pack(anchor="w", pady=(3, 0))

        ttk.Label(parent, text="固定信息与可变信息拼接结果", style="Section.TLabel").pack(anchor="w", pady=(0, 7))
        self.command_preview = ScrolledText(parent, height=9, state="disabled", wrap="word", bg=self.TERMINAL, fg="#8bd0ff", relief="flat", borderwidth=0, padx=12, pady=10, font=("Cascadia Mono", 10))
        self.command_preview.pack(fill="x", pady=(0, 10))
        action_row = ttk.Frame(parent, style="Panel.TFrame")
        action_row.pack(fill="x", pady=(0, 11))
        self.write_run_button = ttk.Button(action_row, text="写入配置并打开 Codex 终端", style="Primary.TButton", command=self._write_and_run)
        self.write_run_button.pack(side="left")
        self.stop_button = ttk.Button(action_row, text="停止 Codex", style="Danger.TButton", state="disabled", command=self._stop_process)
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Label(action_row, textvariable=self.command_status, style="Muted.TLabel").pack(side="left", padx=(15, 0))

        ttk.Label(parent, text="运行日志（Codex 交互界面在新终端）", style="Section.TLabel").pack(anchor="w", pady=(0, 7))
        self.output = ScrolledText(parent, state="disabled", wrap="word", bg=self.TERMINAL, fg="#d8e4f2", relief="flat", borderwidth=0, padx=12, pady=10, font=("Cascadia Mono", 9))
        self.output.pack(fill="both", expand=True)

    def _entry(self, parent: ttk.Frame, label: str, variable: tk.StringVar, side: bool = False) -> None:
        if side:
            frame = ttk.Frame(parent, style="Panel.TFrame")
            frame.grid(row=0, column=1, sticky="ew", padx=(8, 0))
            parent.columnconfigure(1, weight=1)
        else:
            frame = ttk.Frame(parent, style="Panel.TFrame")
            frame.pack(fill="x", pady=(0, 8))
        ttk.Label(frame, text=label, style="Muted.TLabel").pack(anchor="w", pady=(0, 3))
        ttk.Entry(frame, textvariable=variable).pack(fill="x")

    def _labeled_combo(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        values: tuple[str, ...],
        column: int,
        readonly: bool = True,
    ) -> None:
        frame = ttk.Frame(parent, style="Panel.TFrame")
        frame.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0))
        parent.columnconfigure(column, weight=1)
        ttk.Label(frame, text=label, style="Muted.TLabel").pack(anchor="w", pady=(0, 3))
        box = ttk.Combobox(frame, textvariable=variable, values=values, state="readonly" if readonly else "normal")
        box.pack(fill="x")
        if variable is self.active_provider:
            self.provider_combo = box

    def _labeled_grid_entry(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        column: int,
        weight: int = 1,
    ) -> None:
        frame = ttk.Frame(parent, style="Panel.TFrame")
        frame.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0))
        parent.columnconfigure(column, weight=weight)
        ttk.Label(frame, text=label, style="Muted.TLabel").pack(anchor="w", pady=(0, 3))
        ttk.Entry(frame, textvariable=variable).pack(fill="x")

    def _bind_reactive_inputs(self) -> None:
        for variable in (self.active_provider, self.active_model, self.reasoning_effort, self.prompt, self.sandbox, self.profile, self.extra_config, self.force_selection):
            variable.trace_add("write", lambda *_: self._refresh_command_preview())
        for variable in (self.provider_id, self.provider_name, self.base_url, self.api_protocol, self.token, self.requires_openai_auth, self.supports_websockets):
            variable.trace_add("write", lambda *_: self._provider_form_changed())
        self.api_protocol.trace_add("write", lambda *_: self._refresh_protocol_hint())
        self._refresh_protocol_hint()

    def _provider_form_changed(self) -> None:
        if hasattr(self, "config_status"):
            self.config_status.set("有未应用的供应商修改")
        if hasattr(self, "test_status") and (
            self.test_thread is None or not self.test_thread.is_alive()
        ):
            self.test_status.set("配置已修改，请重新测试")

    def _provider_from_form(self) -> Provider:
        provider_id = self.provider_id.get().strip()
        if not provider_id or not re.fullmatch(r"[A-Za-z0-9_-]+", provider_id):
            raise ValueError("Provider ID 无效：只允许字母、数字、下划线和连字符")
        if any(
            index != self.selected_provider and item.provider_id == provider_id
            for index, item in enumerate(self.providers)
        ):
            raise ValueError(f"Provider ID 重复：{provider_id}")
        api_protocol = self.api_protocol.get().strip().lower()
        if api_protocol not in API_PROTOCOLS:
            raise ValueError("API 协议只能选择 OpenAI 或 Anthropic")
        return Provider(
            provider_id=provider_id,
            name=self.provider_name.get().strip() or provider_id,
            base_url=self.base_url.get().strip(),
            wire_api="responses",
            token=self.token.get(),
            requires_openai_auth=self.requires_openai_auth.get(),
            supports_websockets=self.supports_websockets.get(),
            api_protocol=api_protocol,
        )

    def _test_provider(self) -> None:
        if self.test_thread is not None and self.test_thread.is_alive():
            return
        try:
            provider = self._provider_from_form()
            model = self.active_model.get().strip()
            endpoint, headers, payload = test_request_payload(provider, model)
        except ValueError as exc:
            messagebox.showerror("无法测试供应商", str(exc))
            return
        self.test_button.configure(state="disabled")
        self.test_status.set(f"测试中：{api_protocol_label(provider.api_protocol)} / {model}")
        self._append_output(
            f"\n[连通测试] {provider.provider_id} · {api_protocol_label(provider.api_protocol)}\n"
            f"POST {redact_secret(endpoint, provider.token)}\n"
        )
        self.test_thread = threading.Thread(
            target=self._run_provider_test,
            args=(provider, model, endpoint, headers, payload),
            daemon=True,
        )
        self.test_thread.start()

    def _run_provider_test(
        self,
        provider: Provider,
        model: str,
        endpoint: str,
        headers: dict[str, str],
        payload: bytes,
    ) -> None:
        started = time.monotonic()
        request = url_request.Request(endpoint, data=payload, headers=headers, method="POST")
        try:
            with url_request.urlopen(request, timeout=TEST_TIMEOUT_SECONDS) as response:
                raw_text = response.read(1_000_000).decode("utf-8", errors="replace")
                status = int(response.status)
            summary = response_summary(provider.api_protocol, raw_text)
            self.test_events.put(
                (
                    "result",
                    {
                        "ok": 200 <= status < 300,
                        "status": status,
                        "summary": redact_secret(summary, provider.token),
                        "elapsed": time.monotonic() - started,
                        "model": model,
                    },
                )
            )
        except url_error.HTTPError as exc:
            try:
                raw_text = exc.read(1_000_000).decode("utf-8", errors="replace")
            except OSError:
                raw_text = str(exc)
            summary = response_summary(provider.api_protocol, raw_text)
            self.test_events.put(
                (
                    "result",
                    {
                        "ok": False,
                        "status": int(exc.code),
                        "summary": redact_secret(summary, provider.token),
                        "elapsed": time.monotonic() - started,
                        "model": model,
                    },
                )
            )
        except (url_error.URLError, TimeoutError, OSError) as exc:
            self.test_events.put(
                (
                    "error",
                    {
                        "summary": redact_secret(str(exc), provider.token),
                        "elapsed": time.monotonic() - started,
                        "model": model,
                    },
                )
            )

    def _refresh_protocol_hint(self) -> None:
        if self.api_protocol.get() == "Anthropic":
            self.protocol_hint.set("Anthropic 原生 Messages API 需经兼容网关转换为 Codex 支持的 Responses API。")
        else:
            self.protocol_hint.set("OpenAI 类型：直接使用 Codex 支持的 Responses API。")

    # ---------- config and providers ----------
    def _load_config_into_ui(self) -> None:
        self.config_data = load_config()
        try:
            original_text = CONFIG_PATH.read_text(encoding="utf-8")
        except OSError:
            original_text = ""
        provider_protocols = provider_protocols_from_text(original_text)
        providers = self.config_data.get("model_providers", {})
        if not isinstance(providers, dict):
            providers = {}
        self.providers = [
            Provider.from_dict(
                str(key),
                value if isinstance(value, dict) else {},
                provider_protocols.get(str(key), API_PROTOCOL_OPENAI),
            )
            for key, value in providers.items()
        ]
        if not self.providers:
            self.providers = [Provider("agentrouter", "AgentRouter", "https://agentrouter.org/v1", token="")]
        active = str(dict_value(self.config_data, "model_provider", self.providers[0].provider_id))
        model = str(dict_value(self.config_data, "model", ""))
        effort = str(dict_value(self.config_data, "model_reasoning_effort", "high"))
        self.active_model.set(model)
        self.reasoning_effort.set(effort)
        self._refresh_provider_list()
        self.provider_combo.configure(values=tuple(item.provider_id for item in self.providers))
        index = next((i for i, item in enumerate(self.providers) if item.provider_id == active), 0)
        self._select_provider_index(index)
        self.config_status.set(f"已读取 {CONFIG_PATH}")

    def _reload_config(self) -> None:
        self._load_config_into_ui()
        self._append_output(f"已重新读取 {CONFIG_PATH}\n")

    def _refresh_provider_list(self) -> None:
        self.provider_list.delete(0, tk.END)
        for item in self.providers:
            self.provider_list.insert(tk.END, f"{item.provider_id}  ·  {item.name}")
        if hasattr(self, "provider_combo"):
            self.provider_combo.configure(values=tuple(item.provider_id for item in self.providers))
        if self.providers:
            self.provider_list.selection_set(self.selected_provider)

    def _select_provider(self, _event: tk.Event | None = None) -> None:
        selection = self.provider_list.curselection()
        if selection:
            self._select_provider_index(selection[0])

    def _select_provider_index(self, index: int) -> None:
        if not self.providers:
            return
        self.selected_provider = max(0, min(index, len(self.providers) - 1))
        item = self.providers[self.selected_provider]
        self.provider_id.set(item.provider_id)
        self.provider_name.set(item.name)
        self.base_url.set(item.base_url)
        self.api_protocol.set("Anthropic" if item.api_protocol == API_PROTOCOL_ANTHROPIC else "OpenAI")
        self.token.set(item.token)
        self.requires_openai_auth.set(item.requires_openai_auth)
        self.supports_websockets.set(item.supports_websockets)
        self.active_provider.set(item.provider_id)
        self.provider_list.selection_clear(0, tk.END)
        self.provider_list.selection_set(self.selected_provider)
        self.provider_list.activate(self.selected_provider)
        self.config_status.set(f"正在编辑供应商 {item.provider_id}")
        self.test_status.set("尚未测试")

    def _add_provider(self) -> None:
        existing_ids = {item.provider_id for item in self.providers}
        number = 1
        while f"provider-{number}" in existing_ids:
            number += 1
        self.providers.append(Provider(f"provider-{number}", "新供应商"))
        self._refresh_provider_list()
        self._select_provider_index(len(self.providers) - 1)

    def _delete_provider(self) -> None:
        if len(self.providers) <= 1:
            messagebox.showwarning("无法删除", "至少保留一个供应商")
            return
        self.providers.pop(self.selected_provider)
        self.selected_provider = max(0, self.selected_provider - 1)
        self._refresh_provider_list()
        self._select_provider_index(self.selected_provider)

    def _apply_provider_form(self) -> None:
        try:
            provider = self._provider_from_form()
        except ValueError as exc:
            messagebox.showerror("供应商配置无效", str(exc))
            return
        self.providers[self.selected_provider] = provider
        self.active_provider.set(provider.provider_id)
        self._refresh_provider_list()
        self._select_provider_index(self.selected_provider)
        self.config_status.set(f"供应商 {provider.provider_id} 已应用，尚未写盘")

    def _config_text(self) -> str:
        try:
            original = CONFIG_PATH.read_text(encoding="utf-8")
        except OSError:
            original = ""
        return merge_config_text(
            original,
            self.providers,
            self.active_provider.get().strip() or self.providers[self.selected_provider].provider_id,
            self.active_model.get().strip(),
            self.reasoning_effort.get().strip() or "high",
        )

    def _write_config(self) -> bool:
        try:
            self._apply_provider_form_silent()
            if not self.active_model.get().strip():
                raise ValueError("模型 ID 不能为空，请填写第三方供应商提供的模型 ID")
            config_text = self._config_text()
            try:
                tomllib.loads(config_text)
            except tomllib.TOMLDecodeError as exc:
                raise ValueError(f"生成的 config.toml 无法解析，未覆盖原文件：{exc}") from exc
            CODEX_HOME.mkdir(parents=True, exist_ok=True)
            if CONFIG_PATH.exists():
                shutil.copy2(CONFIG_PATH, CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".bak"))
            CONFIG_PATH.write_text(config_text, encoding="utf-8")
        except (OSError, ValueError) as exc:
            messagebox.showerror("写入失败", str(exc))
            return False
        self.config_status.set(f"已写入 {CONFIG_PATH}（旧文件已备份）")
        self._append_output(f"写入配置完成：{CONFIG_PATH}\n")
        return True

    def _apply_provider_form_silent(self) -> None:
        provider = self._provider_from_form()
        self.providers[self.selected_provider] = provider
        self.active_provider.set(provider.provider_id)

    # ---------- command construction and process ----------
    def _command_args(self) -> list[str]:
        fixed, variable = self._command_parts()
        return fixed + variable

    def _command_parts(self) -> tuple[list[str], list[str]]:
        active = self.active_provider.get().strip()
        model = self.active_model.get().strip()
        effort = self.reasoning_effort.get().strip()
        fixed = [CODEX_COMMAND]
        if self.force_selection.get():
            fixed += ["-c", f"model_provider={toml_string(active)}", "-m", model, "-c", f"model_reasoning_effort={toml_string(effort)}"]
        variable: list[str] = []
        if self.sandbox.get().strip():
            variable += ["--sandbox", self.sandbox.get().strip()]
        if self.profile.get().strip():
            variable += ["--profile", self.profile.get().strip()]
        for item in self.extra_config.get().split():
            if "=" not in item:
                continue
            variable += ["-c", item]
        if self.prompt.get().strip():
            variable.append(self.prompt.get().strip())
        return fixed, variable

    def _refresh_command_preview(self) -> None:
        if not hasattr(self, "command_preview"):
            return
        fixed, variable = self._command_parts()
        args = fixed + variable
        render = lambda items: " ".join(self._quote_cmd_arg(item) for item in items) or "（空）"
        self.command_preview.configure(state="normal")
        self.command_preview.delete("1.0", tk.END)
        self.command_preview.insert(tk.END, "固定块\n" + render(fixed) + "\n\n")
        self.command_preview.insert(tk.END, "可变块\n" + render(variable) + "\n\n")
        self.command_preview.insert(tk.END, "最终命令\n" + render(args))
        self.command_preview.configure(state="disabled")
        self.command_status.set("命令已实时生成")

    @staticmethod
    def _quote_cmd_arg(value: str) -> str:
        if not value or re.search(r"[\s\"]", value):
            return '"' + value.replace('"', '\\"') + '"'
        return value

    def _write_and_run(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self.process = None
        if not self._write_config():
            return
        args = self._command_args()
        self._append_output("\n$ " + " ".join(self._quote_cmd_arg(item) for item in args) + "\n")
        try:
            creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            self.process = subprocess.Popen(
                args,
                cwd=str(Path.cwd()),
                creationflags=creation_flags,
                close_fds=True,
            )
        except OSError as exc:
            self.process = None
            messagebox.showerror("启动 Codex 失败", f"找不到 codex.cmd 或启动失败：\n{exc}")
            return
        self.write_run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.command_status.set(f"Codex 已在新终端运行，PID {self.process.pid}")
        self._append_output(f"已打开 Codex 交互终端，PID {self.process.pid}\n")

    def _poll_process(self) -> None:
        try:
            while True:
                kind, payload = self.test_events.get_nowait()
                self.test_thread = None
                self.test_button.configure(state="normal")
                elapsed = float(payload.get("elapsed", 0.0))
                if kind == "result":
                    status = int(payload.get("status", 0))
                    summary = str(payload.get("summary", ""))
                    if payload.get("ok"):
                        self.test_status.set(f"连通成功 · HTTP {status} · {elapsed:.2f}s")
                        self._append_output(f"[成功] HTTP {status} · {elapsed:.2f}s · {summary}\n")
                    else:
                        self.test_status.set(f"测试失败 · HTTP {status} · {elapsed:.2f}s")
                        self._append_output(f"[失败] HTTP {status} · {elapsed:.2f}s · {summary}\n")
                else:
                    summary = str(payload.get("summary", "未知错误"))
                    self.test_status.set(f"连接失败 · {elapsed:.2f}s")
                    self._append_output(f"[连接失败] {elapsed:.2f}s · {summary}\n")
        except queue.Empty:
            pass
        process = self.process
        if process is not None:
            return_code = process.poll()
            if return_code is not None:
                self.process = None
                self.write_run_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                self.command_status.set(f"Codex 已退出，返回码 {return_code}")
                self._append_output(f"Codex 进程已退出，返回码 {return_code}\n")
        self.after(100, self._poll_process)

    def _stop_process(self) -> None:
        process = self.process
        if process is None:
            return
        self.command_status.set("正在停止 Codex…")
        try:
            if hasattr(subprocess, "CREATE_NEW_CONSOLE"):
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                process.terminate()
        except OSError:
            pass

    def _append_output(self, text: str) -> None:
        if not hasattr(self, "output"):
            return
        self.output.configure(state="normal")
        self.output.insert(tk.END, text)
        self.output.see(tk.END)
        self.output.configure(state="disabled")

    def _close(self) -> None:
        if self.process is not None:
            self._stop_process()
        self.destroy()


if __name__ == "__main__":
    CodexConfigStudio().mainloop()
