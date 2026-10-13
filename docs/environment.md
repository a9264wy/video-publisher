# 本机环境注意事项

> 这台 Windows 机器上的坑，与 video-publisher 直接相关的部分。

## Python

- 项目必须用**项目内 `.venv/Scripts/python.exe`**（WorkBuddy 托管 Python 没有 playwright）。
- 离线 wheel 在 `.wheels/`（`scripts/fetch_wheels.py` 抓取）。

## 浏览器

- 复用系统 Chrome（`credentials.channels_web.channel = "chrome"`），免下载 ~150MB 内核。
- playwright 启动参数带 `--no-proxy-server`（`no_proxy: true`）：
  视频号上传是几 MB 起步的大请求，走代理极易卡死；登录/页面加载等小请求看不出问题。

## 代理

- 本机常驻 FlClash：内核外部控制器 `127.0.0.1:9090` 无鉴权，
  `GET /configs` 查 `tun.enable`。
- 工作区 shell 环境里 `http_proxy=http://127.0.0.1:57305`（WorkBuddy 出口代理）。
- 写「探测连通性」的代码要显式决定走不走代理，别只看有没有抛异常——
  `requests` 可能被代理劫持并拿到 502。

## 被安全策略拉黑的命令

- `reg.exe` 被拉黑 → 查注册表用 PowerShell。
- `schtasks.exe` 被拉黑 → Windows 计划任务只能让用户在**管理员 PowerShell** 里手动跑
  `scripts/register_task.ps1 -Time 07:00 -Count 3`。
  本批次已走平台原生定时，本地调度器非必需。

## 硬件/系统坑（装依赖时可能遇到）

- CPU 是 Intel i7-13700HX，**没有 AVX-512**：按 AVX-512 编译的预编译 wheel
  （已知 polars、dlib）加载即 `Illegal instruction`（退出码 132），
  Python 层无法捕获。排查：`python -c "import 包名"` 看退出码。
- 系统 VC++ 运行库处于「半升级」损坏状态（System32 下 msvcp140/vcruntime140
  是 2017 旧版，其余是 2022 新版），VS2022 编译的程序可能莫名崩溃。
  已自带新版 CRT 的程序不受影响。

## 目录约定

- 内容源目录在**项目外**（如 `H:\去1qa\少儿科普白版-…`），由 `config.json → source_dir` 指向；
  每条目含视频、双封面（3:4 竖 + 4:3 横）、`发布.md`（标题+描述+话题）。
- `state/`、`logs/`、`reports/` 是运行时产物，已在 `.gitignore` 排除，不要提交。
