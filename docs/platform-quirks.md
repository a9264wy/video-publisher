# 平台侧硬事实（2026-10 实测校准）

> 本文记录视频号助手网页端（channels.weixin.qq.com）的实测行为与排查结论。
> 改 `channels_web.py` 前必读；每条结论都有踩坑代价，别再重复验证。

## 1. 上传能力受平台/账号限制 —— 先跑 preflight

视频号助手在传字节前调
`POST https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/helper/helper_upload_params`
取上传凭证。返回非 0（本项目实测恒为 `{"errCode":300002,"errMsg":"request failed"}`）时：

- 页面永远停在「0% + 取消上传」
- 封面一直「生成中」
- 点「发表」被弹「请上传视频」

**已排除的怀疑对象**（都验证过，别再查一遍）：

| 怀疑对象 | 排除方式 |
|---|---|
| 浏览器自动化指纹 | `webdriver` 已为 false |
| 系统代理/FlClash | TUN 关闭；直连与走代理结果一致 |
| 网络路径 | 纯 HTTP 重放同样失败 |
| 登录态 | 同一 cookie 调 20+ 个只读接口全部 errCode=0 |
| 视频文件本身 | 编码/分辨率/可解码性均正常 |
| 请求参数与请求头 | 12 种变体全部 300002 |

→ 结论：**账号/服务端侧限制**。

排查工具：

- `python main.py preflight`（约 1 秒，退出码 0=可发布 / 1=不可发布）
- `scripts/replay_upload_params.py`（纯 HTTP 重放，证明与浏览器无关）

发布流程已内置 `_gate_upload_ready()` 前置门，确凿失败秒级拦下，不会傻等 90 秒。

## 2. 页面结构

- 表单在 **Shadow DOM** 内，必须用 playwright `locator`；原生 `querySelector` / `inner_text` 采不到。
- 视频描述是 `div.input-editor`（`data-placeholder="添加描述"`），需真实键盘事件输入。
- 提交按钮：`button:text-is('发表')` / `保存草稿` / `手机预览`。
  ⚠️ `button.weui-desktop-btn_primary` 页面上有 23 个（多为隐藏），按类名取 `.first` 必点错。
- 直接 `goto /platform/post/create` 会被重定向回首页，需先加载后台再做客户端路由
  （`history.pushState` + `popstate`），失败再逐个点侧边栏菜单项。
- 「定时发表」radio 的 `input` 被 CSS 挪到屏幕外（x≈-139897），**只能点 label**；
  时间列表选择器存**容器**（`ol.weui-desktop-picker__time__hour`），
  取项时拼 `容器 li:text-is('08')`。
- 需要枚举多个元素时用 `_locators_any` / `_locators`（`_locate` 返回 `.first`，
  对它 `.count()` 恒为 1，会把日历月份枚举废掉）。

## 3. 封面：4:3 点不开的双层根因（重大修复，2026-10-14）

### 根因一：hover 先行

两个「编辑」按钮都带 `pointer-events: none`，点击生效依赖**先 hover** 让卡片弹出
img-popover 浮层。`locator.click(force=True)` 对 4:3 卡无效（:hover 未生效就点，
落在浮层出现前的卡片上）；3:4 卡因带 `edit-btn-zIndex` 类侥幸能中，
造成「只有 4:3 有 bug」的误判。

**正解**：`page.mouse.move(cx,cy)` → 停 800ms → `page.mouse.click(cx,cy)`。

### 根因二：素材浮层拦截

3:4 封面上传成功后平台产生「素材」，再点另一张卡会弹
「使用此素材作为封面?」浮层（按钮：**直接编辑** / 使用素材），
必须点**直接编辑**才能进真弹窗。点「使用素材」会把 3:4 竖图塞给 4:3 横卡——绝不能点。

### 认按钮方式

按归属卡片文本（`closest('[class*=cover]…')` innerText 含「个人主页卡片 3:4」/
「分享卡片 4:3」）精确匹配，不再按 x 坐标猜。

### 封面区布局（iframe 坐标）

| 区域 | 坐标 |
|---|---|
| `vertical-cover-wrap` | [681,185,74,98] |
| `horizon-cover-wrap` | [771,185,128,98] |
| 编辑按钮（3:4 / 4:3） | [681,265,74,18] / [771,265,128,18] |
| `cover-preview-wrap` | [681,185,544,98] |

封面编辑弹窗在 post/create iframe 内，`_visible_cover_dialog_text` 必须 **iframe 优先扫**。

### 上传完成判据（曾经判错导致假成功）

- 唯一判据 = **可见的「取消上传」**（`_has_visible`，`count()` 必须配 `is_visible()`，
  DOM 常驻隐藏「重新上传」否则 2 秒误报失败）；「重新上传/上传失败」是失败态，分开判；
  完成 = 取消上传消失 + 2s 双重确认。
- ⚠️ 发表页常年存在 0%/100%/333% 等**隐藏 NN% 模板文本**，绝不可作判据
  （旧版用 NN% 兜底导致收敛永不满足，看门狗把成功误判成「90s 卡死」）。
- ⚠️ UI 上传浮层**不是立刻出现的**（投递后数秒才渲染），不能因为「没看到上传态」
  就断定上传完成——必须先等它出现（bootstrap 阶段）。

## 4. 默认参加活动

- 下拉项显示「账号名 · 活动名」，自家活动 = 「白版讲清楚 · 白版说知边看边学」。
- 泛词搜不到时自动降级**全名搜**（必中）。
- 结果需连续两次快照一致再点；点击后轮询回读，不符回滚「不参与」。
- **每条退出路径收起下拉**（Escape 须逐 frame 发；否则挡住「定时」radio）。

## 5. 平台中途弹窗

- 「实名核验」弹窗：只点 ×。
- 「绑定带货者」弹窗：只点「我知道」，**绝不能点「去绑定」**。
- 统一由 `_dismiss_blocking_dialogs` 处理。

## 6. 平台原生「定时发表」优先

- 比本地轮询可靠（不依赖本机开机）。
- **设完时间必须回读校验**，不一致直接抛错中止——否则点「发表」会变成「立即发布」，
  内容当场发出去且不可撤销。
- 注意它和「保存草稿」互斥。

## 7. 提交成功判据

`_submit` 只验证「没被拒」，不验证真的发出。判据看
`state/submit_dumps/submit_after.json`——提交后跳 `/platform/post/list`，
frame 文本出现新卡片 + 「将于XXXX年XX月XX日 08:00发表」。

## 8. 定时作品 objectId 获取与删除

- `post_list`（`userpageType=11`）HTTP 直查**只返回已发表**，拿不到定时作品。
- 定时作品数据在管理页 iframe 自发的 post_list 响应里，用 playwright 拦
  `post/post_list` 响应取得（见 `scripts/find_scheduled.py`）。
- 删除：`POST /micro/content/cgi-bin/mmfinderassistant-bin/post/post_delete`
  body `{"objectId":"export/UzFf..."}`。
- ⚠️ `scripts/delete_scheduled.py` 只删 `.post-feed-item` 文本以指定标题开头
  **且含「将于」字样**的卡片，绝不碰已发表内容。

## 9. 账号内容甄别

「白版讲清楚」账号 10-07 起每天 3 条已发表内容（《狼来了》《滥竽充数》等，
100~340 阅读）是**用户自有内容，不是本批次**，不能删。

## 10. 账号信息

- **正确账号 =「白版讲清楚」**（sph9OGLGHYrqngS，6 粉）——与内容目录「少儿科普白版」对应。
- 旧账号 `用户0284223142`（昵称「预感」）才是被 300002 拒发上传参数的——
  **卡一整天的根因是登错号**。
- 登录态：`state/channels_storage.json`，仅 2 个 cookie（`sessionid` / `wxuin`），
  寿命约 2.5 小时。
