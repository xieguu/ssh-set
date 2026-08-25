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
