# 标准运维流程

> 从零开始发布一批视频到视频号的完整操作手册。
> 页面级硬事实见 `platform-quirks.md`，本机环境注意见 `environment.md`。

## 0. 环境准备（一次性）

```bash
# 项目内 venv（托管 Python 没有 playwright，必须用这个）
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # 或从 .wheels 离线装
```

`config.json → credentials.channels_web.channel = "chrome"` 复用系统 Chrome，免下载内核。

## 1. 登录（一次性，掉线再扫）

```bash
.venv/Scripts/python.exe main.py login
```

弹二维码 → 手机扫码 → 登录态存 `state/channels_storage.json`（寿命约 2.5 小时）。
批量发布前若距上次登录超过 2 小时，先重新 login。

## 2. 发布前预检（每次必跑，约 1 秒）

```bash
.venv/Scripts/python.exe main.py preflight
```

退出码 0 = 可发布，1 = 不可发布（上传凭证被拒，见 platform-quirks.md 第 1 节）。
**不可发布时不要尝试发布**，页面只会傻等在 0%。

## 3. 单条发布

```bash
# 平台原生定时（推荐）：明天 08:00
.venv/Scripts/python.exe main.py publish --item "01-风是从哪来的" --scheduled-at "2026-10-15 08:00"

# 强制重发某条（跳过「已发布」跳过逻辑）
.venv/Scripts/python.exe main.py publish --item "01-风是从哪来的" --force --scheduled-at "2026-10-15 08:00"
```

每条成功日志应包含：`已参加活动：…`、`位置已重置为「不显示位置」`、
`封面已上传（3:4）…（确认=True）`、`封面已上传（4:3）…（确认=True）`、`已提交（模式=scheduled）`。
缺任何一项都要停下来查，不要批量继续。

## 4. 批量排期（隔天发 N 条的通用模式）

参照 `scripts/batch_redo.sh` / `scripts/batch_redo2.sh` 的写法：
一个 shell 数组列条目，循环逐条 `publish --item … --force --scheduled-at …`，
逐条检查 exit code，全部完成输出 `ALL DONE`。

**排期节奏**：手动指定每条 `--scheduled-at`，用平台原生定时，
不要依赖本机开机（本地调度器只是兜底）。

## 5. 发布后核验（三件套，缺一不可）

```bash
# ① 权威核验（分页拉全，推荐）：定时清单 + 每条的定时时间
.venv/Scripts/python.exe scripts/verify_all_scheduled.py

# ② UI 层：回读当前**第一页**卡片「将于XXXX年XX月XX日 08:00发表」
.venv/Scripts/python.exe scripts/verify_schedule_ui.py

# ③ 提交侧：看每次提交后的页面快照
cat state/submit_dumps/submit_after.json
```

### ⚠️ 核验陷阱：只看第一页会误判「条目消失」

管理页 UI 与 `find_scheduled.py`（拦页面自身响应）**都只覆盖第一页 20 条**。
新批次发布后，旧条目会被挤到第二页，看起来像「不见了」——2026-10-15 就被这个
虚惊过一次（当时以为 10-16~10-19 的 10 条丢了）。

**判断定时作品必须用 `verify_all_scheduled.py`**，它按 `currentPage/pageSize`
翻页拉全（接口 `totalCount` 一并带出）。

### 接口字段备忘

- 定时发表时间字段是 **`effectiveTime`**（Unix 秒），不是 `publishTime`；
  **无 `effectiveTime` = 已发表**（或非定时对象）。
- `status` 恒为 1，**不能**用它区分定时/已发表，要看 `effectiveTime` 是否在未来。
- `find_scheduled.py` 的用途是拿定时作品的 **objectId**（删除凭据），
  条目标题在 `desc.shortTitle[0].shortTitle`。

## 6. 人工预览

```bash
# 有头浏览器带登录态进管理页，挂住供人工查看（默认 2 小时）
.venv/Scripts/python.exe scripts/open_preview.py
.venv/Scripts/python.exe scripts/open_preview.py 1800   # 挂 30 分钟
```

## 7. 删除误排的定时作品

```bash
.venv/Scripts/python.exe scripts/delete_scheduled.py 潮汐   # 按标题前缀删
```

只删文本以指定标题开头且含「将于」的卡片；已发表内容不碰。
删除后核对管理页「视频 (N)」计数变化。

## 8. 常见故障速查

| 现象 | 原因 | 处理 |
|---|---|---|
| preflight 退出码 1 / 页面永远 0% | 账号被拒发上传凭证（300002） | 换正确账号重新扫码；已排除浏览器/网络/文件因素 |
| 封面弹窗打不开 | hover 先行 + 素材浮层拦截 | 已修复；确认 `_hover_click_cover_edit` 生效 |
| 定时没设上就提交 | 时间回读校验被绕过 | 已改为不一致即抛错中止；检查定时回读逻辑 |
| 提交后列表没有新卡片 | `_submit` 只验证「没被拒」 | 看 `submit_after.json`；必要时 `--force` 重发 |
| 定时作品删不掉 | HTTP post_list 拿不到定时 objectId | 用 `find_scheduled.py`  playwright 拦截响应 |
| 登录态失效 | 超过 2.5 小时 | 重新 `main.py login` |

## 9. 测试

```bash
.venv/Scripts/python.exe -m unittest discover -s tests
```

纯标准库，无第三方依赖。含「源码级护栏」用例，防止已修的关键判据回退
（如 NN% 不可作上传判据、定时必须回读校验等）。
