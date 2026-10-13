# videopub · 微信视频号内容发布模块

一个可直接运行的视频号发布模块：接收**视频文件 / 标题 / 描述 / 封面图 / 话题标签**，
调用发布通道完成上传，返回**发布结果与详细状态信息**，带完整的异常捕获与错误处理，
支持**每日早上自动发布 3 个视频**的定时任务，并提供**日志记录与结果查询**。

- 运行环境：Python 3.9+（核心零第三方依赖，仅用标准库）
- 源目录：`H:\去1qa\少儿科普白版-20261013-20261013T083033Z-1-001\少儿科普白版-20261013`
- 默认后端：`dryrun`（演练，不联网、无副作用）——拿到真实凭证后再切 `openapi` / `channels_web`

---

## 1. 快速开始

```bash
cd video-publisher

python main.py doctor                 # 环境 + 源目录 + 后端自检
python main.py scan                   # 查看 10 个待发布条目（标题/封面/话题/描述）
python main.py publish --limit 3      # 演练发布 3 条
python main.py preflight              # ★ 上传能力预检：平台现在肯不肯收视频（约 1 秒）
python main.py schedule --once --force # 模拟「每日早上」任务跑一次
python main.py status                 # 状态总览
python main.py history --limit 20     # 发布历史查询
python main.py report --open          # 生成并打开 HTML 报表
```

> **先跑 `preflight`**。视频号助手的上传接口
> （`helper/helper_upload_params`）会时不时对某些账号直接返回 `errCode=300002`，
> 此时上传永远停在 0%，且**与本机网络/代理/自动化无关**。
> 详见 [§4 上传能力预检](#-上传能力预检非常重要先跑这个)。

---

## 2. 目录约定（源目录）

模块按如下约定读取内容，**只读源目录，绝不修改**：

```
<源目录>/
  01-风是从哪来的/
    final.mp4            # 视频（必填）
    发布.md               # 文案：标题 / 简介 / 话题标签（缺省则用目录名兜底）
    封面-3x4.png          # 竖版封面
    封面-4x3.png          # 横版封面（视频号首选，由 cover_preference 决定）
  02-海水为什么是咸的/
    ...
```

`发布.md` 中只解析 **`## 视频号`** 小节：

```
## 视频号（横版 4:3 封面）
```
《风是从哪来的》      ← 第一个代码块首行 = 标题
空气在跑步
```
简介：
```
风看不见摸不着…        ← 「简介：」之后的代码块 = 描述
#白版 #少儿科普        ← #标签 = 话题
```
```

> 解析器会**忽略代码块内部的 `#话题`**，避免把 `#白版` 误判成 Markdown 标题而截断段落。
> 其它平台文案（小红书 / B站 / 公众号）会被保留在 `item.meta["sections"]` 里备用。

---

## 3. 配置（`config.json`）

| 配置节 | 关键项 | 说明 |
|---|---|---|
| 顶层 | `source_dir` | 待发布内容源目录 |
| | `dry_run` | `true`=演练模式（强制走 dryrun） |
| `schedule` | `time` / `daily_count` / `weekdays` / `jitter_seconds` | 每日发布时刻、条数、星期、随机抖动 |
| `publish` | `backend` | `dryrun` / `openapi` / `channels_web` |
| | `publish_mode` | `draft` 存草稿 / `immediate` 立即 / `scheduled` 定时 |
| | `max_retries` / `retry_backoff_seconds` | 重试次数与指数退避基数 |
| | `stop_on_error` / `skip_published` | 失败即停 / 跳过已发布 |
| `content` | `cover_preference` | `4x3`（横版，视频号首选）/ `3x4` |
| | `default_topics` / `append_topics` | 默认话题与自动追加 |
| `limits` | `max_video_size_mb` 等 | 平台硬性限制，用于**发布前预校验** |
| `credentials` | `openapi.*` / `channels_web.*` | 各后端的凭据与端点 |

配置做了**严格校验**：未知字段、非法时间、非法条数都会在启动时直接报 `[ECONFIG]` 并指出字段名，
不会带着错误配置跑到一半才炸。

---

## 4. 三种发布后端

| 后端 | 是否联网 | 依赖 | 用途 |
|---|---|---|---|
| `dryrun` | 否 | 无 | 默认。完整走校验→分片→上传→封面→提交的流程骨架，验证目录结构与调度 |
| `openapi` | 是 | 无（urllib） | 调用「取令牌 → 上传素材 → 提交发布」形态的开放接口，端点/字段全部可配 |
| `channels_web` | 是 | playwright | 自动化 `channels.weixin.qq.com`（视频号助手）网页端，支持扫码登录 |

### 关于「视频号发布接口」的现实说明

视频号对**个人创作者没有公开的「发布视频」开放接口**（开放平台的能力面向服务商且需账号授权）。
因此本模块把「发布接口」抽象成可插拔后端：

- 若你有服务商 / 企业网关：把地址填进 `credentials.openapi`，用 `--backend openapi --live`；
- 若你是个人号：用 `channels_web` 走网页自动化（这是目前个人号自动发布的通行做法）。

```bash
python main.py login --channel chrome --timeout 600   # 弹浏览器扫码，登录态存到 state/channels_storage.json
python main.py login --force                          # 已有登录态但想换号时，强制重新扫码
python main.py doctor --live --backend channels_web   # 自检：确认账号与登录态
python main.py publish --limit 3 --live --backend channels_web
```

- `login` 会先检测已有登录态：**仍然有效就直接复用**，不会重复弹二维码；
- 登录成功后会打印账号（如 `用户0284223142 / 视频号ID: 视频28`）便于核对；
- 二维码会自动截图到 `state/login_qr.png`，即使看不到浏览器窗口也能扫到图。

#### 已实测校准的真实页面事实（2026-10）

这三条是**踩坑后才确认**的，直接影响能否跑通：

1. **表单在 Shadow DOM 内** —— 必须用 playwright 的 `locator`（能穿透），
   原生 `page.query_selector` / `inner_text('body')` **采不到任何控件**，会拿到空 DOM。
   代码里已全部改为 locator，并有单测断言防止回退。
2. **视频描述是 `div.input-editor`**（`data-placeholder="添加描述"`），不是 textarea；
   短标题才是 `input[placeholder*='短标题']`。输入必须用真实键盘事件（`keyboard.type`），
   直接改 DOM 不会被框架识别。
3. **平台原生支持「定时发表」**（页面上有 不定时 / 定时 两个 radio）。
   `publish_mode="scheduled"` 时模块会优先使用它 —— 平台侧定时不受本机是否开机影响，
   比纯本地轮询可靠。
4. 直接 `goto /platform/post/create` 会被**重定向回首页**，必须先加载后台首页再做
   客户端路由跳转（`history.pushState` + `popstate`）。
5. 上传控件真实 accept 为 `video/mp4,video/x-m4v,video/*`；提交按钮为
   `button.weui-desktop-btn_primary`（发表）/ `保存草稿` / `手机预览`。
6. **判断「上传完成」必须用 DOM 结构，不能靠抓页面文本**：
   投递文件后 3 秒页面上就有 `NN%` 进度元素，但 `frame.inner_text('body')` 与
   自写的文本遍历（只收 div/span/p… 叶子节点、且有 800 条上限）**都取不到「取消上传」**。
   早期版本据此误判「上传已完成」→ 提交被平台弹「请上传视频」挡回 → 结果仍记成
   success（**假成功**）。现在改为 `locator("text=取消上传")` + `NN%` 进度双重判据，
   并加了「进度冻结 90s 即判卡死」的看门狗。

页面改版时，用只读探查脚本重新采集（**不会在你的账号上产生任何内容**）：

```bash
python scripts/inspect_publish_page.py   # dump DOM 到 state/inspect_create.json + 截图
```

#### ⚠️ 上传能力预检（非常重要，先跑这个）

视频号助手在真正传字节之前，会先调
`POST /cgi-bin/mmfinderassistant-bin/helper/helper_upload_params` **取上传凭证**。
这个接口一旦返回非 0，客户端就永远拿不到凭证，页面表现为：

```
视频缩略图区：0%（进度条纹丝不动）+「取消上传」
封面预览：个人主页卡片 / 分享卡片 一直「生成中」
点「发表」→ 平台弹「请上传视频」把提交挡回来
```

**而且它与本机环境无关**（2026-10-13 实测结论，别再重复排查）：

| 怀疑对象 | 实测结果 |
| --- | --- |
| 自动化指纹 `navigator.webdriver` | 已开启 stealth（`webdriver=false`），无效 |
| 系统代理 / Clash（FlClash） | `tun.enable=false`；直连与走代理结果一致 |
| 网络路径 | 用保存的 cookie 走**纯 HTTP**（不经浏览器）重放，同样 300002 |
| 登录态 | 同一 cookie 调 `auth_data` / `statistic/*` 等只读接口**全部成功** |
| 视频文件本身 | H.264(avc1)+AAC、faststart、1920×1080、浏览器可正常解码取帧 |
| 请求参数/头 | 逐项对照 12 种变体（uin、appType、UA、路径、referer…）全部 300002 |

结论：**属账号 / 服务端侧限制**，任何客户端调参都绕不开。

所以上传前先花 ~1 秒问清楚：

```bash
python main.py preflight            # 不开浏览器，直接问平台「现在肯不肯收视频」
python main.py preflight --direct   # 怀疑代理干扰时，绕过所有代理直连再问一次
```

```text
✅ 可以发布
  登录态文件：state\channels_storage.json
  [ok ] storage_state
  [ok ] login errCode=0
  [ok ] upload_params errCode=0
  账号：预感 / 视频号：用户0284223142（作品 28 粉丝 0）
```

```text
❌ 暂不可发布
  ...
  [FAIL] upload_params errCode=300002 request failed
  结论：平台拒绝下发上传参数…属账号/服务端层面限制
```

`preflight` 退出码：可用 `0`，不可用 `1`，可直接用于脚本前置判断。

若 `preflight` 报 300002，可行的人工排查顺序：

1. 在**你自己的浏览器**里手动登视频号助手，手动传一个视频 ——
   若手动也传不上，就是账号/平台侧，请带账号 + 时间点联系视频号助手客服；
2. 重新扫码登录（`python main.py login --force`），排除登录态不完整；
3. 上传接口是**已知的高频故障点**，视频号助手社区里「进度卡 0% / 网络错误 300002」
   的反馈常年存在，换网络、换浏览器、退出重登都有人试过且不一定有效。

### 环境准备（channels_web 后端）

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install playwright

# 推荐：复用系统已装 Chrome/Edge，免下载 ~150MB 内核
#   config.json → credentials.channels_web.channel = "chrome" | "msedge"
# 或使用 playwright 自带内核：
.venv/Scripts/python.exe -m playwright install chromium
```

> 若 pip 无法解析索引（本机曾遇到 `ERROR: ... (from versions: none)`），
> 可用内置的离线抓取脚本绕过：
> `python scripts/fetch_wheels.py playwright pyee==13 greenlet typing-extensions`
> 然后 `pip install --no-index --find-links=.wheels playwright`。

---

## 5. 定时发布

### 方式 A：常驻进程（模块自带调度器）

```bash
python main.py schedule --count 3 --time 08:00
```

- 每日 `08:00`（可配星期、抖动）自动发布 **3 条**；
- 到点执行 → 发完继续睡到下一个时刻；
- `Ctrl+C` 优雅退出（当前任务跑完再退）；
- 调度状态落在 `state/schedule_state.json`，**进程重启也不会同日重复发布**；
- 单次任务失败不会让调度器退出（自愈），并将异常写入结构化日志。

### 方式 B：交给系统计划任务（推荐长期运行）

```powershell
# 管理员 PowerShell
.\scripts\register_task.ps1 -Time 08:00 -Count 3
```

注册的是 `run_daily.bat`，即 `python main.py schedule --once`：
每次唤醒只判断「此刻是否该发」，幂等由状态文件保证，重复唤醒不会重复发。
支持 `StartWhenAvailable`（错过时间会补跑）。

---

## 6. 日志与结果查询

| 产物 | 位置 | 内容 |
|---|---|---|
| 文本日志 | `logs/videopub.log` | 轮转日志（5MB × 7），人读 |
| 结构化日志 | `logs/publish.jsonl` | 每事件一行 JSON：`publish_start` / `publish_result` / `batch_done` … |
| 状态索引 | `state/publish_state.json` | 每个条目的最新状态（幂等判断依据），原子写 |
| 历史记录 | `state/history.jsonl` | 每次尝试一行，**只追加**，可审计 |
| HTML 报表 | `reports/发布报表-*.html` | 自包含报表：状态卡片、近 14 天活动、可筛选的历史表 |

查询命令：

```bash
python main.py status                  # 发布总览 + 定时状态 + 待发布队列
python main.py history --status failed # 只看失败
python main.py history --limit 100 --json
python main.py report --open           # 生成可视化报表
```

---

## 7. 异常与错误处理

统一异常体系（全部继承 `PublisherError`，带稳定 `code` 与结构化 `detail`）：

| 错误码 | 异常 | 触发场景 | 是否重试 |
|---|---|---|---|
| `ECONFIG` | `ConfigError` | 配置非法 / 未知字段 / 时间越界 | — |
| `ESCAN` | `ScanError` | 源目录不存在 / 无子目录 | — |
| `ECONTENT` | `ContentParseError` | 发布文案解析失败（自动降级用目录名） | — |
| `EVALID` | `ValidationError` | 视频缺失/格式不支持/过大过小、描述超长、话题超限 | 否（**联网前拦截**） |
| `EAUTH` | `AuthError` | 登录态失效 / 凭证错误 | 否（需人工介入） |
| `EUPLOAD` | `UploadError` | 网络异常 / 上传失败 / 5xx | **是**（指数退避） |
| `ERATELIMIT` | `RateLimitError` | 平台限流 | **是** |
| `EREJECT` | `RemoteRejectError` | 内容被审核拒绝 | 否 |
| `EDEPENDENCY` | `DependencyError` | 缺少可选依赖（如 playwright） | 否 |
| `ESTATE` / `ESCHEDULE` | `StateError` / `ScheduleError` | 状态文件异常 / 无匹配发布时刻 | — |

设计要点：

1. **预校验前置**：文件、体积、格式、文案长度在联网前全部校验完，把可预知失败挡在门外；
2. **可重试与不可重试分开**：网络/限流退避重试，鉴权/审核类立即失败（省时间也省配额）；
3. **异常绝不炸批**：单条失败只影响该条，未预期异常也会被兜底为 `EUNKNOWN` 并记入历史；
4. **状态文件自愈**：`publish_state.json` 损坏会自动备份为 `*.corrupt-<ts>.json` 并重建；
5. **写操作原子**：状态文件采用「写临时文件 + `os.replace`」，避免断电产生半截文件；
6. **定时器自愈**：到点任务异常只记录不退出；同日重复触发被幂等抑制。

---

## 8. 作为库调用

```python
from videopub import Config, VideoPublisher, scan_source

cfg = Config.load("config.json")
with VideoPublisher(cfg) as pub:                 # 默认 dryrun
    items = scan_source(cfg)
    result = pub.publish_one(items[0])           # 单条
    print(result.status.label, result.remote_id, result.error_code)

    report = pub.run(limit=3, trigger="api")     # 批量
    print(f"成功 {len(report.succeeded)} / 失败 {len(report.failed)}")
```

---

## 9. 测试

```bash
python tests/test_error_handling.py       # 46 个用例
```

覆盖配置校验、源目录异常、单条校验、重试与退避、不可重试快速失败、幂等与强制重发、
状态存储与损坏自愈、定时器时间计算与同日去重、后端依赖缺失的友好报错等。

---

## 10. 文件结构

```
video-publisher/
├── main.py                     # 入口（python main.py <命令>）
├── config.json                 # 配置
├── requirements.txt
├── videopub/
│   ├── __init__.py             # 对外导出
│   ├── cli.py                  # 命令解析与各子命令
│   ├── config.py               # 配置加载 / 校验 / 脱敏
│   ├── content.py              # 发布.md 文案解析
│   ├── scanner.py              # 源目录扫描 → VideoItem
│   ├── validators.py           # 发布前校验（含 PNG/JPEG 尺寸解析）
│   ├── models.py               # VideoItem / PublishResult / PublishStatus
│   ├── errors.py               # 异常体系与错误码
│   ├── logger.py               # 文本 + JSONL 双路日志
│   ├── store.py                # 状态索引 + 追加型历史
│   ├── publisher.py            # 编排：重试 / 幂等 / 批次报告
│   ├── scheduler.py            # 定时发布（守护 / --once）
│   ├── report.py               # 状态面板 + HTML 报表
│   └── backends/
│       ├── base.py             # 后端抽象
│       ├── dryrun.py           # 演练
│       ├── openapi.py          # 开放接口（urllib）
│       └── channels_web.py     # 视频号助手网页自动化
├── scripts/
│   ├── run_daily.bat           # 计划任务用
│   └── register_task.ps1       # 一键注册 Windows 计划任务
├── docs/                       # 实战经验文档（改代码前必读）
│   ├── platform-quirks.md      # 平台侧硬事实：上传门/页面结构/封面双层根因/定时校验
│   ├── operations.md           # 标准运维流程：登录→预检→发布→核验→删除
│   └── environment.md          # 本机环境注意事项（venv/代理/schtasks 黑名单等）
├── tests/test_error_handling.py
├── logs/  state/  reports/     # 运行时产物
```
