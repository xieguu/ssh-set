# Nexus SSH

一个本地运行的可视化 SSH 客户端，使用 Tkinter 绘制界面，Paramiko 建立 SSH 连接。

## 启动

```powershell
python -m pip install -r requirements.txt
python app.py
```

Windows 也可以双击 `run.bat`。

## 功能

- 密码认证和私钥认证
- 交互式 SSH shell、命令历史、Ctrl+C、中断和快捷命令
- SSH 命令解析：可直接粘贴 `ssh -D 1080 root@47.88.77.200`
- 动态转发执行：本机开启 SOCKS5 监听，连接通过 SSH Transport 转发
- 支持 `-D`、`-N`、`-p`、`-l`、`-i`、`-T`、`-t`、`-o ConnectTimeout=秒` 和 `-o ServerAliveInterval=秒`
- 连接配置保存到 `%APPDATA%\\NexusSSH\\profiles.json`
- 密码与私钥口令只保存在当前进程内，不写入配置文件
- 显示远端 Host key 类型和 SHA-256 指纹

## 粘贴命令执行

在左侧“命令解析 / 执行”输入框粘贴：

```text
ssh -D 1080 root@47.88.77.200
```

点击“解析”后，程序会回填主机、端口和用户名，并显示执行摘要；点击“执行”才会发起连接。连接成功后，本机 `127.0.0.1:1080` 是 SOCKS5 代理，可供浏览器、代理工具或其他支持 SOCKS5 的程序使用。点击“停止命令”会关闭 SSH 会话和本地监听。

默认绑定 `127.0.0.1`，只允许本机访问；如果显式写成 `-D 0.0.0.0:1080`，程序会在日志中提示该代理可能被局域网其他设备访问。

如果只需要代理、不需要远端 shell，使用：

```text
ssh -N -D 1080 root@47.88.77.200
```

解析器不会调用本地 shell，也不会执行粘贴内容里的 PowerShell/cmd 管道或重定向；不支持的 SSH 参数会直接报错。当前版本暂不支持 `-L`、`-R`、`-J` 等其他转发模式。

首次连接未知主机时，为了方便本地使用，程序会接受该 Host key，并把指纹显示在右侧会话信息中。生产环境使用前应当与服务器管理员提供的指纹核对。

## Codex 配置可视化

`codex_config_gui.py` 是一个不依赖第三方包的单文件 Tkinter 工具，用于编辑当前用户的 `~/.codex/config.toml`：

- 可添加、删除、修改任意第三方 `model_providers`，Bearer Token 按明文显示；
- API 协议提供 `OpenAI` 与 `Anthropic` 两种类型；当前 Codex 原生只接受 Responses API，Anthropic 原生接口需要兼容网关转换；
- 模型 ID 是自由输入框，不限制 OpenAI，可填写供应商提供的任意精确模型 ID；
- “连通测试”会按当前表单向 OpenAI `/v1/responses` 或 Anthropic `/v1/messages` 发送最小请求，显示 HTTP 状态、耗时和响应摘要，Token 不会写入日志；
- 固定参数（provider、model、reasoning）与可变参数（sandbox、profile、额外 `-c`、prompt）分栏预览；
- 按“编辑供应商 → 写入配置 → 拼接命令 → 运行 Codex”的流程执行；
- 写盘前自动生成 `config.toml.bak`，并保留原配置中的项目、插件、MCP 等无关区块。

启动：

```powershell
python codex_config_gui.py
```

工具会直接调用 `codex.cmd` 并打开独立终端，保留 Codex TUI 的正常交互；主窗口显示拼接命令、PID 和退出状态。Token 只会写入用户自己的 Codex 配置，不要把 `config.toml` 或备份文件提交到仓库。
