# Nexus SSH

独立的本地可视化 SSH 客户端。本目录里的代码只负责 SSH，不读取或修改 Codex 配置。

## 启动

双击 `run.bat`，或者：

```powershell
python -m pip install -r requirements.txt
python run.py
```

## 功能

- 密码或私钥认证；
- 交互式 SSH shell；
- 解析并执行 `ssh -N -D 1080 root@example.com`；
- 建立本地 SOCKS5 动态代理；
- 保存不包含密码的连接配置。
