# Codex Config Studio

独立的本地 Codex 配置可视化工具。本目录里的代码只负责 Codex 配置，不加载 SSH 客户端或 Paramiko。

## 启动

双击 `run.bat`，或者：

```powershell
python run.py
```

`codex_config_gui.py` 本身仍是可直接运行的单文件程序，并且不依赖第三方 Python 包。

## 功能

- 编辑 `~/.codex/config.toml`；
- 添加自定义模型供应商和模型 ID；
- 测试 OpenAI/Anthropic 风格接口；
- 预览固定参数与可变参数拼接结果；
- 写入配置后独立启动 `codex.cmd`。
