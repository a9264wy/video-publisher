"""视频号助手网页自动化后端（可选依赖 playwright）。

现实约束：视频号对个人创作者**没有**公开的「发布视频」开放接口，官方开放能力
面向服务商且需账号授权。个人号每日自动发布，业界通行做法是自动化
``channels.weixin.qq.com``（视频号助手）网页端。

因此本后端用浏览器自动化实现同一套发布语义，并解决自动化最棘手的两件事：
  1. **登录态**：通过 ``login`` 一次性扫码登录，持久化 ``storage_state``（cookies+localStorage），
     之后每次发布复用；登录态失效时抛 :class:`AuthError` 提示重新登录。
  2. **页面结构漂移**：所有选择器集中在 ``SELECTORS`` 且可在配置里覆盖，
     页面改版只需改配置，不动代码。

依赖：``pip install playwright && playwright install chromium``
未安装时本后端会抛 :class:`DependencyError`（不重试），不影响 dryrun / openapi 使用。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path
from typing import Any

from .. import timing
from ..config import Config
from ..errors import (
    AuthError,
    DependencyError,
    PublisherError,
    RemoteRejectError,
    UploadError,
)
from ..logger import get_logger
from ..models import PublishResult, PublishStatus, VideoItem
from ..validators import validate_item
from .base import PublishBackend

log = get_logger("backend.channels_web")

CREATE_URL = "https://channels.weixin.qq.com/platform/post/create"
HOME_URL = "https://channels.weixin.qq.com/platform"
LOGIN_URL = "https://channels.weixin.qq.com/login.html"

# 「取上传参数」接口的路径片段。它是**能否开始上传**的唯一开关：
# 返回非 0（实测 300002）时，页面永远停在 0%，且与本机网络/代理/自动化指纹无关。
UPLOAD_PARAM_HINT = "helper/helper_upload_params"

# 登录成功后微信会下发这些 cookie（用于可靠判定登录态，比看 DOM 稳）
LOGIN_COOKIE_HINTS = ("sessionid", "wxuin", "bizuin", "slave_sid", "sessionid_ch")

# 集中管理选择器，**已按 2026-10 真实页面校准**（由 scripts/inspect_*.py 采集）。
#
# 三个关键事实（实测得到，别凭想象写）：
#   1. 表单被渲染了两份：主 frame 的 **Shadow DOM** 里一份、iframe
#      ``micro/content/post/create`` 里一份。**主 frame 的 Shadow 副本是活的那份**
#      （截图已证实点击生效），而 playwright locator 恰好能穿透 shadow root。
#   2. 视频描述是 ``div.input-editor``（data-placeholder="添加描述"），不是 textarea；
#      短标题才走 input。
#   3. 「定时发表」的 radio ``input`` 被 CSS 挪到 ``x≈-139897``（屏幕外），
#      ``check()`` 会报 "Element is outside of the viewport"，**只能点 label**。
SELECTORS: dict[str, list[str]] = {
    # 上传：真实 accept 为 "video/mp4,video/x-m4v,video/*"，input 隐藏但可直接投递
    "file_input": ["input[type=file][accept*='video']", "input[type=file]"],
    # 视频描述编辑器
    "desc_editor": [
        ".post-desc-box .input-editor",
        "div.input-editor",
        "[data-placeholder='添加描述']",
        "div[contenteditable='true']",
    ],
    "desc_box": [".post-desc-box"],
    # 短标题（独立输入框，可选）
    "short_title": ["input[placeholder*='短标题']"],
    # 封面（视频上传后才会出现独立的图片 file input）
    "cover_input": ["input[type=file][accept*='image']"],
    # 提交按钮（真实文案：保存草稿 / 手机预览 / 发表）。
    # ⚠️ ``button.weui-desktop-btn_primary`` 在页面上有 **23 个**（绝大多数是隐藏的），
    #    按类名取 .first 会点错；必须用 ``:text-is()``（全等）锁死文案。
    "publish_btn": ["button:text-is('发表')", "button.weui-desktop-btn_primary:text-is('发表')"],
    "draft_btn": ["button:text-is('保存草稿')"],
    "preview_btn": ["button:text-is('手机预览')"],
    # 定时发表：「不定时 / 定时」两个 radio（input 在屏幕外 → 点 label）
    "schedule_radio_on": [
        '.post-time-wrap label:has(input[value="1"])',
        ".post-time-wrap label:text-is('定时')",
    ],
    "schedule_radio_off": [
        '.post-time-wrap label:has(input[value="0"])',
        ".post-time-wrap label:text-is('不定时')",
    ],
    "schedule_checked": ['.post-time-wrap input[value="1"]'],
    # 定时时间：外层只读输入框（placeholder=请选择发表时间）
    "schedule_time": ["input.weui-desktop-form__input[placeholder='请选择发表时间']"],
    # 弹出面板：外层 dd 是「日期面板」，时间列表 dd 嵌在它里面
    "schedule_day_panel": [".post-time-wrap dd.weui-desktop-picker__dd"],
    "schedule_month_panel": [".weui-desktop-picker__panel_day"],
    "schedule_month_label": [".weui-desktop-picker__panel_day .weui-desktop-picker__panel__label"],
    "schedule_next_month": [".weui-desktop-picker__panel_day .weui-desktop-btn__icon__right"],
    "schedule_prev_month": [".weui-desktop-picker__panel_day .weui-desktop-btn__icon__left"],
    "schedule_day_link": [".weui-desktop-picker__panel_day a"],
    # 时间列表（点击面板内的小输入框才展开）。
    # ⚠️ 这里存的是 **容器** 选择器，取项时拼 ``<容器> li:text-is('08')``。
    #    曾经把 ``ol … li`` 直接当选择器、再用 ``_locate()`` 取 ``.first``，
    #    拿到的是一个 li，再往它里面找 li 永远找不到 —— 这就是实测翻车的原因。
    "schedule_time_list_input": ["dl.weui-desktop-picker__time input[placeholder='请选择时间']"],
    "schedule_hour_list": ["ol.weui-desktop-picker__time__hour"],
    "schedule_minute_list": ["ol.weui-desktop-picker__time__minute"],
    # 侧边栏菜单项（进入「发表动态」页用；实测为 56x56 等高排列，「发表动态」是第 2 项）
    "menu_link": ["a.finder-ui-desktop-menu__link", ".finder-ui-desktop-menu__link"],
    # 状态判断
    "login_qr": [        "img.qrcode", "canvas.qrcode", ".login-qrcode img", ".qrcode img",
        "#qrcode img", "img[src^='data:image']", ".login-scan img", "canvas",
    ],
    "upload_progress": [".upload-progress", ".progress-bar"],
    # 「上传中」的唯一可靠信号是 **「取消上传」**（只在上传进行时存在）。
    # ⚠️ 2026-10-13 再修：「重新上传 / 上传失败」是**失败态**，不能与上传中混判；
    #    且页面常年存在 0%/100%/333% 等与上传无关的 NN% 文本（隐藏模板元素），
    #    任何「兜底把任意 NN% 当上传态」的写法都会让收敛判定永远不满足
    #    —— 实测后果：上传明明已完成，看门狗却报「90s 进度停在 0%」误判卡死。
    "upload_cancel": ["text=取消上传"],
    "upload_fail": ["text=重新上传", "text=上传失败"],
    "upload_state": ["text=取消上传", "text=重新上传", "text=上传失败"],

    # 活动参与（发布时默认参加指定活动，如「白版说知边看边学」）。
    # 入口：表单「活动」行的 activity-display（默认文案「不参与活动」），
    # 点开后是带「搜索活动」输入框的下拉，搜索结果项为 .activity-item。
    "activity_display": ["div.activity-display-wrap", "div.activity-display"],
    "activity_search": [".activity-filter-wrap input.weui-desktop-form__input",
                        ".activity-filter-wrap input[type='text']"],
    "activity_item": [".activity-item"],
    # 平台弹窗（实名核验 / 绑定带货账号等）——挡在表单上层时必须关掉。
    # 只点关闭类控件（× / 我知道了）；**绝不能点「去绑定」**（会跳走）。
    "dialog_close": [
        ".weui-desktop-dialog__wrp button.weui-desktop-dialog__close",
        ".weui-desktop-dialog__close",
        ".weui-desktop-dialog__wrp .weui-desktop-icon__close",
        ".finder-common-dialog .weui-desktop-btn_default",
    ],
    "dialog_ok_btn": [
        ".weui-desktop-dialog__wrp button:has-text('我知道了')",
        ".weui-desktop-dialog__wrp button:has-text('我知道了去绑定') >> button:has-text('我知道了')",
    ],

    "uploading_hint": ["text=上传中", "text=正在上传", "text=处理中", "text=转码中"],
    "done_flag": ["text=发表成功", ".post-success"],
}


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise DependencyError(
            "channels_web 后端需要 playwright，请执行：pip install playwright 且 playwright install chromium",
            detail={"hint": "也可改用 backend=dryrun / openapi"},
        ) from exc
    return sync_playwright


class ChannelsWebBackend(PublishBackend):
    name = "channels_web"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        creds = (config.credentials or {}).get("channels_web", {}) or {}
        self.storage_state = Path(
            creds.get("storage_state") or (config.resolve("state") / "channels_storage.json")
        )
        self.headless = bool(creds.get("headless", True))
        self.slow_mo = int(creds.get("slow_mo_ms", 120))
        self.upload_timeout_ms = int(creds.get("upload_timeout_ms", 15 * 60 * 1000))
        # 「上传态」最长允许停留多久而不见任何进展（毫秒）。
        # 视频号先调 helper_upload_params 拿上传凭证，拿不到时页面会**一直**
        # 停在「0% / 取消上传」。这个看门狗就是用来把「卡死」和「就是慢」区分开的。
        # 注意：进度**有变化**时会重置计时，所以只有「进度冻结」才会触发；
        # 实测 4MB 视频真上传时百分比秒级就在跳，90s 纹丝不动必是卡死。
        self.upload_stall_timeout_ms = int(creds.get("upload_stall_timeout_ms", 90 * 1000))
        # 投递文件后，最多等多久才允许判定「从未出现上传态」。
        # 关键：UI 的上传浮层不是立刻出现的（实测 ~5–11s 才渲染），
        # 早期版本在 3~6s 时看不到浮层就以为「上传已完成」，直接导致假成功。
        self.upload_start_timeout_ms = int(creds.get("upload_start_timeout_ms", 120 * 1000))
        self.selectors = {**SELECTORS, **(creds.get("selectors") or {})}
        # 浏览器内核：留空 = playwright 自带 chromium（需先 playwright install chromium）；
        # 填 "chrome" / "msedge" 则复用系统已安装的浏览器，免下载 ~150MB 内核。
        self.channel = str(creds.get("channel", "") or "")
        # 直连开关：本机若常驻 Clash / FlClash 之类的系统代理，
        # Chromium 会默认走 WinINET 代理 —— 小请求没事，但**大文件上传会卡死在 0%**。
        # 实测：不开直连时视频投递后进度 2 分钟纹丝不动，平台回「请上传视频」。
        self.no_proxy = bool(creds.get("no_proxy", False))
        # 反自动化指纹：playwright 默认带 --enable-automation，会让
        # navigator.webdriver === true。视频号助手的媒体处理 SDK 会据此
        # 拒绝处理（表现：FFmpeg-WASM 载入后即报错、上传永远 0%），
        # 但页面 UI 正常渲染，非常有迷惑性。默认开启，可关。
        self.stealth = bool(creds.get("stealth", True))
        self.qr_path = Path(creds.get("qr_path") or (config.resolve("state") / "login_qr.png"))
        # 提交现场留档目录（截图 + 各 frame 文案），排查「提交了但没生效」用
        self._dump_dir = config.state_path.parent / "submit_dumps"
        try:
            self._dump_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._dump_dir = config.state_path.parent
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        # 上传凭证接口的失败现场（见 _attach_upload_probe）
        self._upload_param_err: int | None = None
        self._upload_param_msg = ""
        self._upload_param_ok = False
        self._probe_attached = False

    # -- 会话 --------------------------------------------------------------
    def _launch(self, *, headless: bool | None = None):
        sync_playwright = _require_playwright()
        if self._pw is None:
            self._pw = sync_playwright().start()
        if self._browser is None:
            kwargs: dict[str, Any] = {
                "headless": self.headless if headless is None else headless,
                "slow_mo": self.slow_mo,
            }
            if self.no_proxy:
                # 绕过系统代理（Clash/FlClash 等）。视频号上传是几 MB 起步的大请求，
                # 走代理极易卡死；登录、页面加载等小请求则看不出问题。
                kwargs.setdefault("args", [])
                kwargs["args"] = list(kwargs["args"]) + ["--no-proxy-server"]
            if self.stealth:
                kwargs.setdefault("args", [])
                kwargs["args"] = list(kwargs["args"]) + [
                    "--disable-blink-features=AutomationControlled",
                ]
                kwargs["ignore_default_args"] = ["--enable-automation"]
            if self.channel:
                kwargs["channel"] = self.channel
            try:
                self._browser = self._pw.chromium.launch(**kwargs)
            except Exception as exc:
                raise DependencyError(
                    f"浏览器启动失败（channel={self.channel or 'chromium'}）：{exc}",
                    detail={
                        "hint": "若用自带内核请先执行 `playwright install chromium`；"
                                "或把 credentials.channels_web.channel 设为 chrome / msedge 复用系统浏览器",
                    },
                ) from exc
        if self._context is None:
            ctx_kwargs: dict[str, Any] = {
                "viewport": {"width": 1440, "height": 900},
                "locale": "zh-CN",
            }
            if self.storage_state.exists():
                ctx_kwargs["storage_state"] = str(self.storage_state)
            self._context = self._browser.new_context(**ctx_kwargs)
            self._page = self._context.new_page()
        return self._context

    # -- 登录态判定 --------------------------------------------------------
    @staticmethod
    def _has_login_cookie(context) -> bool:
        try:
            cookies = context.cookies()
        except Exception:
            return False
        for c in cookies or []:
            name = str(c.get("name", "")).lower()
            if any(h in name for h in LOGIN_COOKIE_HINTS):
                return True
        return False

    def _capture_qr(self, page, path: Path) -> str:
        """把登录二维码截图存盘（供无法直接看窗口时扫码）。

        三级策略（页面改版也能兜住）：
          1. 按选择器精确截取二维码元素；
          2. 几何启发式：找出「接近正方形、边长 >= 120px、位于页面右半侧」的元素
             并用 ``clip`` 截图 —— 实测视频号登录页的二维码容器没有可用的 class，
             只能靠几何特征定位；
          3. 兜底整页截图，至少保证用户拿到能扫的图。
        """
        path.parent.mkdir(parents=True, exist_ok=True)

        # 1) 选择器（用 locator，其 screenshot() 同样能穿透 Shadow DOM）
        for sel in self.selectors.get("login_qr", []):
            try:
                loc = page.locator(sel).first
                if loc.count() == 0 or not loc.is_visible():
                    continue
                box = loc.bounding_box()
                if not box or box.get("width", 0) < 60 or box.get("height", 0) < 60:
                    continue
                loc.screenshot(path=str(path))
                return str(path)
            except Exception:
                continue

        # 2) 几何启发式
        try:
            cands = page.evaluate(
                """() => {
                  const out = [];
                  document.querySelectorAll('img,canvas,div,svg').forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width < 120 || r.height < 120) return;
                    const ratio = r.width / r.height;
                    if (ratio < 0.85 || ratio > 1.18) return;
                    if (r.x < window.innerWidth * 0.35) return;   // 二维码在右侧面板
                    if (r.width > window.innerWidth * 0.7) return; // 排除整页容器
                    out.push({x: r.x, y: r.y, w: r.width, h: r.height});
                  });
                  out.sort((a, b) => b.w * b.h - a.w * a.h);
                  return out.slice(0, 5);
                }"""
            )
            if cands:
                c = cands[0]
                pad = 12
                page.screenshot(path=str(path), clip={
                    "x": max(c["x"] - pad, 0), "y": max(c["y"] - pad, 0),
                    "width": c["w"] + pad * 2, "height": c["h"] + pad * 2,
                })
                log.info("已按几何特征定位二维码：%.0fx%.0f", c["w"], c["h"])
                return str(path)
        except Exception as exc:
            log.debug("几何定位二维码失败：%s", exc)

        # 3) 兜底
        try:
            fallback = path.with_name("login_page.png")
            page.screenshot(path=str(fallback), full_page=False)
            log.info("未定位到二维码元素，已保存整页截图：%s", fallback)
            return str(fallback)
        except Exception:
            return ""

    # Shadow DOM 感知的文本采集：
    # playwright 的 inner_text('body') 只读主文档，而视频号助手的界面在 Shadow DOM 内，
    # 直接读会得到空串 —— 曾导致「账号信息抓不到」+「内容审核拦截检测失效」。
    _TEXT_JS = """() => {
      const out = [];
      const seen = new Set();
      function walk(root, depth) {
        if (!root || depth > 12) return;
        let nodes = [];
        try { nodes = [...root.querySelectorAll('div,span,p,li,a,label,h1,h2,h3'); } catch (e) { return; }
        for (const el of nodes) {
          if (el.children.length !== 0) continue;
          const t = (el.textContent || '').trim();
          if (t && !seen.has(t)) { seen.add(t); out.push(t); }
          if (out.length > 800) return;
        }
        let hosts = [];
        try { hosts = [...root.querySelectorAll('*')]; } catch (e) { return; }
        for (const el of hosts) if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
      }
      walk(document, 0);
      return out.join('\\n');
    }"""

    def _page_text(self, page) -> str:
        """聚合所有 frame（含 Shadow DOM）的可见文本。

        为什么要这么麻烦：``inner_text('body')`` 既穿不过 Shadow DOM，
        也看不到 iframe 内的表单（发表动态页的表单就在 iframe 里）。
        """
        chunks: list[str] = []
        for frame in self._frames(page):
            try:
                txt = frame.evaluate(self._TEXT_JS)
            except Exception:
                txt = ""
            if not txt:
                try:
                    txt = frame.inner_text("body")
                except Exception:
                    txt = ""
            if txt:
                chunks.append(txt)
        return "\n".join(chunks)

    def _account_info(self, page) -> dict[str, Any]:
        """从已登录的页面抓取账号标识，便于用户确认登录的是哪个号。"""
        info: dict[str, Any] = {}
        text = self._page_text(page)
        if not text:
            return info
        m = re.search(r"视频号ID[:：]\s*(\S+)", text)
        if m:
            info["channels_id"] = m.group(1)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for i, ln in enumerate(lines):
            if ln in ("申请认证", "关于腾讯微信视频号运营规范问题咨询") and i > 0:
                cand = lines[i - 1]
                if cand and not cand.isdigit():
                    info["nickname"] = cand
                break
        m2 = re.search(r"关注者\s*(\d+)", text)
        if m2:
            info["followers"] = int(m2.group(1))
        return info

    def login(self, *, timeout_seconds: int = 600, qr_path: str | None = None,
              force: bool = False) -> dict[str, Any]:
        """扫码登录。

        若已有**仍然有效**的登录态且 ``force=False``，直接复用并返回
        ``already_logged_in=True`` —— 避免每次都要重新扫码（实测：登录态还在时
        仍弹二维码，会让人以为没登上）。
        """
        if not force and self.storage_state.exists():
            try:
                info = self.authenticate()
                log.info("检测到仍然有效的登录态，无需重新扫码")
                return {"ok": True, "already_logged_in": True, "account": {
                    k: v for k, v in info.items() if k in ("channels_id", "nickname", "followers")
                }, "storage_state": str(self.storage_state), "url": info.get("url", "")}
            except AuthError:
                log.info("已有登录态已失效，转为扫码登录")

        target_qr = Path(qr_path) if qr_path else self.qr_path
        ctx = self._launch(headless=False)
        page = self._page or (ctx.pages[0] if ctx.pages else ctx.new_page())
        try:
            # 能走到这里，说明没有**可用**登录态（首次登录 / 旧态失效 / force 重登），
            # 所以一律先清 cookie。必须清：残留的过期 cookie 会让下面的扫码等待循环
            # 立刻判定「已扫码」跳出，只等 40s 就报 not_redirected —— 实测连续踩 3 次，
            # 用户根本没机会扫二维码（首次登录与 force 分支原本是清的，漏了失效回退分支）。
            try:
                ctx.clear_cookies()
            except Exception:
                pass
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            log.warning("打开视频号助手失败（继续尝试）：%s", exc)
        page.wait_for_timeout(2500)

        log.info("请在浏览器窗口中用微信扫码登录（最长等待 %d 秒）…", timeout_seconds)
        saved_qr = self._capture_qr(page, target_qr)
        if saved_qr:
            log.info("登录二维码已保存：%s", saved_qr)
        else:
            log.info("未能自动截取二维码，请直接查看浏览器窗口")

        deadline = time.time() + timeout_seconds
        ticks = 0
        scanned = False
        while time.time() < deadline:
            if self._has_login_cookie(ctx):
                scanned = True
                break
            page.wait_for_timeout(1000)
            ticks += 1
            if ticks % 30 == 0:  # 二维码有有效期，页面会自动刷新，定期重截
                again = self._capture_qr(page, target_qr)
                if again:
                    saved_qr = again
                log.info("仍在等待扫码…（已等待 %ds）", ticks)

        if not scanned:
            log.warning("等待扫码超时，未获取到登录态")
            return {"ok": False, "error": "timeout", "qr": saved_qr,
                    "hint": "请重新执行 login 并尽快扫码"}

        # 拿到登录 cookie ≠ 页面已跳转完成。必须等到离开登录页，
        # 否则会把「仍在 login.html」误报成登录成功（实测踩过）。
        log.info("已获取登录 cookie，等待跳转到管理后台…")
        settle_deadline = time.time() + 40
        while time.time() < settle_deadline:
            page.wait_for_timeout(1000)
            if "login" not in page.url:
                break
        if "login" in page.url:
            try:
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(6000)
            except Exception as exc:
                log.warning("跳转后台超时：%s", exc)

        # 轮询等待账号面板渲染，带回「登录的是哪个号」
        account: dict[str, Any] = {}
        for _ in range(15):
            page.wait_for_timeout(1000)
            account = self._account_info(page)
            if account.get("channels_id") or account.get("nickname"):
                break
        logged_in = "login" not in page.url

        self.storage_state.parent.mkdir(parents=True, exist_ok=True)
        ctx.storage_state(path=str(self.storage_state))
        cookies = [c.get("name") for c in (ctx.cookies() or [])]

        if not logged_in:
            log.warning("已收到登录 cookie，但页面仍停留在登录页，登录态可能不完整")
            return {"ok": False, "error": "not_redirected", "qr": saved_qr,
                    "storage_state": str(self.storage_state),
                    "url": page.url, "cookies": cookies,
                    "hint": "请重试；若反复出现，可能是账号未完成视频号开通"}

        log.info("登录成功：%s（登录态已保存 %s）",
                 account.get("nickname") or account.get("channels_id") or "已登录",
                 self.storage_state)
        return {
            "ok": True,
            "account": account,
            "storage_state": str(self.storage_state),
            "qr": saved_qr,
            "cookie_count": len(cookies),
            "cookies": cookies,
            "url": page.url,
        }

    def _is_logged_in(self, page) -> bool:
        """是否已登录。

        以 URL 为准：对照实验表明
          * 未登录访问 /platform → 302 到 /login.html
          * 已登录访问 /platform → 停留在 /platform
        比「找二维码元素」可靠得多（后者在平台页可能被无关 canvas 误命中）。
        """
        try:
            return "login" not in (page.url or "")
        except Exception:
            return False

    def authenticate(self) -> dict[str, Any]:
        if not self.storage_state.exists():
            raise AuthError(
                f"未找到视频号登录态文件: {self.storage_state}",
                detail={"hint": "先执行 `python main.py login` 扫码登录一次"},
            )
        ctx = self._launch()
        page = self._page or (ctx.pages[0] if ctx.pages else ctx.new_page())
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            log.warning("打开视频号助手超时（继续校验）：%s", exc)
        # SPA 需要几秒才完成鉴权跳转，轮询等待
        for _ in range(12):
            page.wait_for_timeout(1000)
            if self._is_logged_in(page):
                break
        if not self._is_logged_in(page):
            raise AuthError(
                "视频号助手登录态已失效，请重新执行 `python main.py login`",
                detail={"storage_state": str(self.storage_state), "url": page.url},
            )
        # 后台是懒加载 SPA，账号面板要过几秒才渲染出来；轮询等待以便带回账号信息
        account: dict[str, Any] = {}
        for _ in range(15):
            page.wait_for_timeout(1000)
            account = self._account_info(page)
            if account.get("channels_id") or account.get("nickname"):
                break
        self._authenticated = True
        self._auth_info = {"mode": "storage_state", "file": str(self.storage_state),
                           "url": page.url, **account}
        log.info("登录态有效：%s", account.get("nickname") or page.url)
        return self._auth_info

    # -- 发布 --------------------------------------------------------------
    def _gate_upload_ready(self) -> None:
        """发布前预检：平台肯不肯下发上传参数。

        只花 ~1 秒（一次 HTTP），却能避免白跑一遍浏览器自动化、白等 90 秒卡死、
        白传几 MB 视频。**判定要「确凿才拦」**：
          * 预检明确拿到非 0 的 ``errCode`` → 直接抛不可重试错误（确定性问题）；
          * 预检自己出错（网络抖动、登录态读取失败）→ 只告警放行，
            把判断交给后面的浏览器内探针，避免误拦正常的发布。
        """
        creds = (self.config.credentials or {}).get("channels_web", {}) or {}
        if not creds.get("preflight_check", True):
            return
        try:
            from ..preflight import check_upload_ready
            rep = check_upload_ready(self.config, timeout=15.0,
                                     direct=bool(creds.get("no_proxy", False)))
        except Exception as exc:  # noqa: BLE001
            log.warning("上传预检未完成（忽略，继续发布）：%s", exc)
            return
        if rep.get("ready"):
            log.info("上传预检通过：平台已正常下发上传参数")
            return
        code = rep.get("upload_err_code")
        if code is None:
            log.warning("上传预检未得出结论（%s），继续尝试发布", rep.get("hint"))
            return
        raise UploadError(
            f"上传预检未通过：平台拒绝下发上传参数（errCode={code}）——"
            f"视频字节不会开始上传，页面会永远停在「0% / 取消上传」。"
            f"这**不是**本机网络、代理或自动化指纹问题（已实测：纯 HTTP 重放同样失败，"
            f"而同一登录态调只读接口全部成功），属账号或服务端侧限制。",
            detail={"errCode": code, "errMsg": rep.get("upload_err_msg", ""),
                    "diagnosis": "platform_refused_upload_params",
                    "account": rep.get("account", {}),
                    "hint": rep.get("hint", "")},
            retryable=False,
        )

    def publish(self, item: VideoItem, *, options: dict[str, Any] | None = None) -> PublishResult:
        opts = options or {}
        result = self._result(item, options={**opts, "backend_name": self.name})
        t0 = time.perf_counter()
        if not self._authenticated:
            self.authenticate()
        validate_item(item, self.config)
        self._gate_upload_ready()

        ctx = self._launch()
        page = self._page or ctx.new_page()
        try:
            self._enter_create_page(page)
            log.info("已进入「发表动态」页")
            self._dismiss_blocking_dialogs(page)
            self._attach_upload_probe(page)

            self._upload_video(page, item)
            self._dismiss_blocking_dialogs(page)
            self._fill_description(page, item)
            activity = str(getattr(self.config.content, "activity", "") or "")
            if activity:
                got = self._join_activity(page, activity)
                result.detail["activity"] = got or f"（未参加成功：{activity}）"
            self._apply_schedule(page, item, opts)
            if item.cover_path and Path(item.cover_path).exists():
                self._upload_cover(page, item)

            result.remote_id = self._submit(page, item)
            result.finish(PublishStatus.SUCCESS)
            result.detail["publish_mode"] = self.config.publish.publish_mode
            if item.meta.get("scheduled_at"):
                result.detail["scheduled_at"] = item.meta["scheduled_at"]
            result.detail["phases"] = ["enter_page", "upload_video", "description",
                                       "activity", "schedule", "cover", "submit"]
        except AuthError:
            raise
        except PublisherError:
            result.duration_ms = int((time.perf_counter() - t0) * 1000)
            result.finish(PublishStatus.FAILED)
            raise
        except Exception as exc:  # 页面异常统一转为可重试的上传错误
            result.duration_ms = int((time.perf_counter() - t0) * 1000)
            result.error_code = "EUPLOAD"
            result.error_message = f"网页自动化失败: {exc}"
            result.finish(PublishStatus.FAILED)
            raise UploadError(result.error_message, detail={"selector_hint": "可覆盖 config.credentials.channels_web.selectors"}) from exc
        result.duration_ms = int((time.perf_counter() - t0) * 1000)
        return result

    def _locators_any(self, page, selectors: list[str]):
        """跨 frame 查找，返回 **未收窄** 的 locator（匹配全部）。

        与 :meth:`_locate_any` 的区别很关键：后者返回 ``.first``。
        对``.first`` 出来的 locator 再调 ``.count()`` 永远是 1，
        也无法枚举 —— 需要「找一堆元素」（如日历月份标签、小时列表项）时必须用本方法。
        """
        for frame in self._frames(page):
            for sel in selectors:
                try:
                    loc = frame.locator(sel)
                    if loc.count() > 0:
                        return loc
                except Exception:
                    continue
        return None

    def _locate_any(self, page, selectors: list[str]):
        """跨 frame 查找任意一组选择器的第一个命中项。"""
        loc = self._locators_any(page, selectors)
        return loc.first if loc is not None else None

    def _locators(self, page, key: str):
        """按选择器键取**未收窄**的 locator（跨 frame）。"""
        return self._locators_any(page, self.selectors.get(key, []))

    def _click_first_visible(self, page, selectors: list[str], *, timeout_ms: int = 5_000):
        """跨 frame 点掉**第一个真正可见**的匹配元素；找不到返回 False。

        为什么不能用 :meth:`_locate_any`：它返回 ``.first``，而页面上隐藏的
        同名/同文案按钮极多（主 frame 的 shadow DOM 里就有一大堆）。
        ``.first`` 很可能命中一个 display:none 的元素 → ``click`` 超时或被跳过，
        于是「二次确认弹窗」永远点不掉，提交静默失败、却仍被判为成功。
        """
        for frame in self._frames(page):
            for sel in selectors:
                try:
                    loc = frame.locator(sel)
                    n = loc.count()
                except Exception:
                    continue
                for i in range(min(n, 12)):
                    cand = loc.nth(i)
                    try:
                        if not cand.is_visible():
                            continue
                        cand.click(timeout=timeout_ms)
                        return True
                    except Exception:
                        continue
        return False

    def _dump_state(self, page, tag: str) -> dict[str, Any]:
        """把提交前后的页面状态落盘（截图 + 各 frame 文案），便于事后定位。

        实盘翻车时最有价值的就是这份现场：提交到底有没有被受理、
        平台有没有弹窗/报错，光看日志是看不出来的。
        """
        info: dict[str, Any] = {"tag": tag, "url": "", "frames": []}
        try:
            info["url"] = page.url
        except Exception:
            pass
        for i, f in enumerate(self._frames(page)):
            try:
                txt = f.evaluate(
                    "() => (document.body ? document.body.innerText : '').trim().slice(0, 4000)"
                )
            except Exception:
                txt = ""
            if txt:
                info["frames"].append({"index": i, "url": getattr(f, "url", ""), "text": txt})
        # 可见按钮文案（帮助确认弹窗按钮叫什么）
        try:
            info["visible_buttons"] = page.evaluate(
                r"""() => {
  const out = [];
  const seen = new Set();
  const stack = [document];
  while (stack.length) {
    const r = stack.pop();
    let all = [];
    try { all = r.querySelectorAll('button'); } catch (e) { continue; }
    for (const el of all) {
      if (el.shadowRoot) stack.push(el.shadowRoot);
      const rect = el.getBoundingClientRect();
      const st = getComputedStyle(el);
      if (rect.width > 0 && rect.height > 0 && st.display !== 'none' && st.visibility !== 'hidden') {
        const t = (el.innerText || '').trim();
        if (t && !seen.has(t)) { seen.add(t); out.push(t.slice(0, 20)); }
      }
    }
  }
  return out.slice(0, 40);
}"""
            )
        except Exception:
            info["visible_buttons"] = []
        try:
            page.screenshot(path=str(self._dump_dir / f"{tag}.png"), full_page=True)
        except Exception:
            pass
        try:
            (self._dump_dir / f"{tag}.json").write_text(
                json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return info

    def _apply_schedule(self, page, item: VideoItem, options: dict[str, Any] | None = None) -> None:
        """使用平台原生的「定时发表」。

        比在本地轮询调度更可靠：平台侧定时**不依赖本机是否开机**。

        踩坑记录（第五次，2026-10）：
          * ``text=定时发表`` 会**子串匹配**到一段不可见的提示文案
            「使用定时发表将无法保存草稿。若要保存草稿，需先取消定时发表。」，
            拿它做定位再 ``.locator("text=定时").last`` 必然命中该隐藏 div → click 超时；
          * 真正的单选项是 ``.post-time-wrap label.weui-desktop-form__check-label``，
            其 ``input[type=radio]`` 被 CSS 挪到 ``x≈-139897``（屏幕外），
            ``check()`` 直接报 ``Element is outside of the viewport`` → **只能点 label**；
          * 选了「定时」之后才渲染出「发表时间」picker。

        ⚠️ **安全关键**：设完时间必须回读校验。若时间没设上，后面点「发表」
        就会变成「立即发布」——本该明天发的内容当场发出去，不可撤销。
        因此校验不一致时**直接抛错中止**，绝不硬着头皮提交。
        """
        opts = options or {}
        want_scheduled = self.config.publish.publish_mode == "scheduled"
        self._set_timing_radio(page, want_scheduled)
        if not want_scheduled:
            return

        override = str(opts.get("scheduled_at") or item.meta.get("scheduled_at") or "")
        target = self.config.publish.resolve_scheduled_at(override=override)
        if target is None:
            log.warning("publish_mode=scheduled 但解析不出定时时间（缺 scheduled_at/daily_time），"
                        "已退回「不定时」以免误发")
            self._set_timing_radio(page, False)
            return

        self._set_schedule_datetime(page, target)

        actual = self._read_schedule_value(page)
        want = target.strftime("%Y-%m-%d %H:%M")
        if actual != want:
            raise UploadError(
                f"平台定时时间设置失败：期望 {want!r}，实际 {actual!r}",
                detail={"expected": want, "actual": actual,
                        "hint": "已中止提交，避免误成立即发布；"
                                "可核对 config.credentials.channels_web.selectors.schedule_*"},
                retryable=False,   # 确定性问题，重试只会白白重传一次视频
            )
        item.meta["scheduled_at"] = want
        log.info("平台原生定时发表已设定：%s", want)

    # -- 定时发表：单选项 --------------------------------------------------
    def _set_timing_radio(self, page, scheduled: bool) -> None:
        """把「定时发表」的单选项切到「定时」或「不定时」。

        非定时模式也要**显式**点回「不定时」：平台会记住上次选择，
        否则草稿/立即发布模式下「保存草稿」按钮会处于不可用状态。
        """
        key = "schedule_radio_on" if scheduled else "schedule_radio_off"
        loc = self._locate(page, key)
        if loc is None:
            if scheduled:
                raise UploadError("未找到「定时」单选项（页面结构可能已改版）",
                                  detail={"key": key}, retryable=False)
            return
        try:
            loc.click(timeout=15_000)
        except Exception as exc:
            if scheduled:
                raise UploadError(f"点击「定时」失败：{exc}", detail={"key": key},
                                  retryable=False) from exc
            log.warning("点回「不定时」失败（可忽略）：%s", exc)
            return
        page.wait_for_timeout(1500)
        if scheduled:
            chk = self._locate(page, "schedule_checked")
            if chk is None or not chk.is_checked():
                raise UploadError("「定时」未被选中（radio 状态未生效）", retryable=False)

    # -- 定时发表：日期时间 ------------------------------------------------
    def _read_schedule_value(self, page) -> str:
        loc = self._locate(page, "schedule_time")
        if loc is None:
            return ""
        try:
            return (loc.input_value() or "").strip()
        except Exception:
            return ""

    def _set_schedule_datetime(self, page, target: dt.datetime) -> None:
        """在「发表时间」picker 上点出 ``target``（日期面板 → 小时 → 分钟）。"""
        year, month, day, hhmm = timing.format_for_picker(target)
        hour, minute = hhmm.split(":")

        box = self._locate(page, "schedule_time")
        if box is None:
            raise UploadError("未找到「发表时间」输入框", retryable=False)
        box.click(timeout=15_000)
        page.wait_for_timeout(2000)

        panel = self._locate(page, "schedule_day_panel")
        if panel is None:
            raise UploadError("「发表时间」面板未弹出（选择器可能已过期）", retryable=False)

        self._goto_month(page, int(year), int(month))

        dloc = panel.locator(f"a:text-is('{day}')")
        if dloc.count() == 0:
            raise UploadError(f"日历中找不到 {year}-{month} 的 {day} 号", retryable=False)
        clicked = False
        for i in range(dloc.count()):
            cls = dloc.nth(i).get_attribute("class") or ""
            if "disabled" not in cls:
                dloc.nth(i).click(timeout=10_000)
                clicked = True
                break
        if not clicked:
            raise UploadError(
                f"{day} 号不可选：平台对可定时的日期范围有限制（通常是未来若干天内）",
                detail={"target": target.isoformat()},
                retryable=False,
            )
        page.wait_for_timeout(1500)
        log.info("日历已选 %s-%s-%s", year, month, day)

        # 时间列表要点击面板里的小输入框才会展开
        tl = self._locate(page, "schedule_time_list_input")
        if tl is None:
            raise UploadError("未找到时间下拉（无法设定 HH:MM）", retryable=False)
        tl.click(timeout=10_000)
        page.wait_for_timeout(1500)
        self._pick_time_item(page, "schedule_hour_list", hour)
        self._pick_time_item(page, "schedule_minute_list", minute)
        log.info("时间列表已选 %s:%s", hour, minute)

        # 点面板外的元素收起面板，让值落到外层只读输入框
        outside = self._locate(page, "desc_box") or self._locate(page, "schedule_radio_on")
        if outside is not None:
            try:
                outside.click(timeout=5_000)
            except Exception:
                pass
        page.wait_for_timeout(1800)

    def _goto_month(self, page, year: int, month: int) -> None:
        """把日历翻到指定年月（默认通常已是当月，但别依赖它）。"""
        for _ in range(36):
            labels = self._panel_labels(page)
            cur = self._parse_month(labels)
            if cur is None:
                log.warning("解析不出日历年月标签 %s，假定已在目标月", labels)
                return
            if cur == (year, month):
                return
            key = "schedule_prev_month" if cur > (year, month) else "schedule_next_month"
            btn = self._locate(page, key)
            if btn is None:
                raise UploadError(f"翻月按钮缺失（{key}），无法翻到 {year}-{month}",
                                  retryable=False)
            try:
                btn.click(timeout=8_000)
            except Exception as exc:
                raise UploadError(f"翻月点击失败：{exc}", detail={"target": f"{year}-{month}"},
                                  retryable=False) from exc
            page.wait_for_timeout(900)
        raise UploadError(f"翻月失败：超过 36 次仍未到 {year}-{month}", retryable=False)

    def _panel_labels(self, page) -> list[str]:
        """日历面板头部的「2026年」「10月」两个标签。

        必须用**未收窄**的 locator 才能枚举到两个；
        早期用 ``_locate()``（返回 ``.first``）只能拿到「2026年」，
        导致月份解析失败、翻月逻辑形同虚设。
        """
        loc = self._locators(page, "schedule_month_label")
        if loc is None:
            return []
        out: list[str] = []
        try:
            n = loc.count()
        except Exception:
            return []
        for i in range(min(n, 6)):
            try:
                out.append((loc.nth(i).inner_text() or "").strip())
            except Exception:
                continue
        return out

    @staticmethod
    def _parse_month(labels: list[str]) -> tuple[int, int] | None:
        y = m = None
        for t in labels:
            if t.endswith("年") and t[:-1].strip().isdigit():
                y = int(t[:-1].strip())
            elif t.endswith("月") and t[:-1].strip().isdigit():
                m = int(t[:-1].strip())
        return (y, m) if (y and m) else None

    def _pick_time_item(self, page, container_key: str, value: str) -> None:
        """在小时/分钟列表里点中 ``value``（跳过 disabled 项）。

        ``container_key`` 存的是**容器**选择器（``ol.weui-desktop-picker__time__hour``），
        这里拼成 ``容器 li:text-is('08')`` 一次性定位到目标项 ——
        ``:text-is()`` 是全等匹配，能避免 ``08`` 误命中 ``08`` 之外的项。
        """
        containers = self.selectors.get(container_key) or []
        tried: list[str] = []
        for container in containers:
            sel = f"{container} li:text-is('{value}')"
            tried.append(sel)
            items = self._locators_any(page, [sel])
            if items is None:
                continue
            n = items.count()
            for i in range(n):
                cls = items.nth(i).get_attribute("class") or ""
                if "disabled" not in cls:
                    items.nth(i).click(timeout=10_000)
                    page.wait_for_timeout(1000)
                    return
            raise UploadError(
                f"{value!r} 在时间列表里是 disabled（平台限制，通常是距现在太近）",
                detail={"key": container_key, "value": value, "tried": tried},
                retryable=False,
            )
        raise UploadError(
            f"时间列表里没有 {value!r}",
            detail={"key": container_key, "value": value, "tried": tried},
            retryable=False,
        )


    def _frames(self, page) -> list:
        """主文档 + 所有子 frame。

        **关键事实（第三次踩坑）**：视频号「发表动态」的表单并不在主页面的
        Shadow DOM 里，而是渲染在独立 iframe
        ``channels.weixin.qq.com/micro/content/post/create`` 中。
        只查主 frame（``page.locator``）永远拿不到控件 —— 必须跨 frame 搜索。
        """
        try:
            frames = list(page.frames)
        except Exception:
            return [page]
        main = page.main_frame
        ordered = [main] + [f for f in frames if f is not main]
        return ordered

    def _locate(self, page, key: str):
        """跨 frame 返回第一个命中的 locator（playwright locator 能穿透 open shadow root）。"""
        loc = self._locators(page, key)
        return loc.first if loc is not None else None

    def _count(self, page, key: str) -> int:
        total = 0
        selectors = self.selectors.get(key, [])
        for frame in self._frames(page):
            for sel in selectors:
                try:
                    total += frame.locator(sel).count()
                except Exception:
                    continue
        return total

    def _wait_for(self, page, key: str, timeout_ms: int) -> bool:
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            if self._locate(page, key) is not None:
                return True
            page.wait_for_timeout(1000)
        return False

    def _enter_create_page(self, page) -> None:
        """进入「发表动态」页。

        实测结论（两次踩坑）：
          * 直接 ``goto /platform/post/create`` 会被重定向回首页；
          * ``history.pushState`` 客户端路由**不稳定**（有时生效、有时被路由回滚）。

        因此改为**点击侧边栏菜单项**这一真实用户路径：菜单项是
        ``a.finder-ui-desktop-menu__link``（56x56 等高排列），「发表动态」为第 2 项。
        为防顺序变化，按候选顺序逐个尝试，以「出现上传控件」为成功判据。
        """
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        for _ in range(15):
            page.wait_for_timeout(1000)
            if self._is_logged_in(page):
                break
        if not self._is_logged_in(page):
            raise AuthError("加载后台时登录态失效", detail={"url": page.url})

        # 等侧边栏渲染出来
        for _ in range(20):
            try:
                if page.locator(self.selectors["menu_link"][0]).count() > 0:
                    break
            except Exception:
                pass
            page.wait_for_timeout(1000)
        page.wait_for_timeout(2000)

        # 先试客户端路由（快），不行再逐个点菜单项（稳）
        try:
            page.evaluate(
                "() => { history.pushState({}, '', '/platform/post/create');"
                " window.dispatchEvent(new PopStateEvent('popstate')); }"
            )
            page.wait_for_timeout(3000)
        except Exception:
            pass
        if self._wait_for(page, "file_input", 8_000):
            log.info("已进入「发表动态」页（客户端路由）")
            return

        links = page.locator(self.selectors["menu_link"][0])
        count = min(links.count(), 10)
        order = [1] + [i for i in range(count) if i != 1]
        for idx in order:
            if idx >= count:
                continue
            try:
                links.nth(idx).click()
            except Exception as exc:
                log.debug("点击菜单项 %d 失败：%s", idx, exc)
                continue
            page.wait_for_timeout(2500)
            # 成功判据是「上传控件真的出现了」——表单在 iframe 内，URL 变化并不代表就绪
            if self._wait_for(page, "file_input", 12_000):
                log.info("已进入「发表动态」页（菜单项 #%d，url=%s）", idx, page.url)
                return
        raise UploadError("无法进入「发表动态」页（页面结构可能已改版）",
                          detail={"url": page.url,
                                  "frames": [f.url for f in self._frames(page)],
                                  "hint": "用 scripts/inspect_publish_page.py 重新采集选择器"})

    def _attach_upload_probe(self, page) -> None:
        """盯住「取上传参数」接口，失败就把现场记下来。

        **为什么要专门盯它**（2026-10 定位到的根因）：
        视频号助手在真正传字节之前，会先 ``POST /cgi-bin/mmfinderassistant-bin/
        helper/helper_upload_params`` 取上传凭证。这个接口一旦返回非 0（实测
        ``{"errCode":300002,"errMsg":"request failed"}``），客户端就永远拿不到凭证，
        页面表现为「进度条 0% + 取消上传」「封面一直生成中」，而**其它一切都正常**：
        文件被接受、本地首帧/封面能算出来、描述能填、日历能选、提交按钮可点。

        早期没有这道判据，代码一路走到点「发表」，被平台弹「请上传视频」挡回来后
        仍然记成 success —— 也就是「假成功」。现在把它变成显式失败。
        """
        if self._probe_attached:
            return
        self._probe_attached = True
        self._upload_param_err = None
        self._upload_param_msg = ""
        self._upload_param_ok = False

        def _on_response(resp) -> None:
            url = ""
            try:
                url = resp.url
                if UPLOAD_PARAM_HINT not in url:
                    return
                # 留一条 INFO 级痕迹：这条日志是判断「探针到底有没有挂上」的唯一依据
                log.info("探针命中上传参数接口：HTTP %s %s", resp.status, url.split("?")[0])
                data = json.loads(resp.body().decode("utf-8", "replace"))
            except Exception as exc:
                if url and UPLOAD_PARAM_HINT in url:
                    log.warning("解析上传参数响应失败：%s（%s）", exc, url[:120])
                return
            code = data.get("errCode")
            if code in (0, None):
                # 客户端会重试；只要有一次成功就清掉告警
                self._upload_param_ok = True
                self._upload_param_err = None
                self._upload_param_msg = ""
                return
            self._upload_param_err = int(code)
            self._upload_param_msg = str(data.get("errMsg", ""))
            log.error("平台未下发上传参数：errCode=%s %s（上传不会开始）",
                      code, self._upload_param_msg)

        try:
            page.on("response", _on_response)
        except Exception as exc:  # pragma: no cover
            log.debug("挂载上传探针失败：%s", exc)

    # 读取页面上「恰好是一个百分数」的小元素（上传进度）。
    # 用 :text-is 级别的等价判断，避免把页面里其它含 % 的文案（如 20% 折扣）算进来。
    _PROGRESS_JS = r"""() => {
      const out = [];
      const seen = new Set();
      const stack = [document];
      while (stack.length) {
        const root = stack.pop();
        let all = [];
        try { all = root.querySelectorAll('*'); } catch (e) { continue; }
        for (const el of all) {
          if (el.shadowRoot && !seen.has(el)) { seen.add(el); stack.push(el.shadowRoot); }
          if (el.children.length) continue;
          const t = (el.textContent || '').trim();
          if (/^\d{1,3}%$/.test(t)) out.push(t);
        }
      }
      return out;
    }"""

    def _upload_progress(self, page) -> list[str]:
        """收集页面上形如 ``NN%`` 的文本（用于判断上传是否真的在动）。"""
        vals: list[str] = []
        for f in self._frames(page):
            try:
                vals.extend(f.evaluate(self._PROGRESS_JS) or [])
            except Exception:
                continue
        return vals

    def _upload_param_failure(self) -> UploadError | None:
        err = self._upload_param_err
        if not err:
            return None
        return UploadError(
            f"平台拒绝下发上传参数（errCode={err}）—— 视频字节不会开始上传，"
            f"页面会永远停在「0% / 取消上传」。"
            f"这**不是**本机网络、代理或自动化指纹问题：同一登录态调只读接口"
            f"（auth_data / statistic 等）全部成功，属账号或服务端侧限制。",
            detail={
                "errCode": err,
                "errMsg": self._upload_param_msg,
                "diagnosis": "platform_refused_upload_params",
                "hint": "① 先跑 `python main.py preflight` 复现；"
                        "② 在浏览器里手动登视频号助手手动传一个视频对照；"
                        "③ 若手动也传不上，请带账号+时间点找视频号助手客服反馈。"
                        "注意：这与本模块代码无关，换后端/换参数都无法绕开。",
            },
            retryable=False,   # 重试只会白传一遍视频
        )

    # 判定「正在上传」的文案。只用文案，不用百分比元素 ——
    # 页面上可能有与上传无关的 `NN%`，用它当判据会误判成「永远在上传」。
    UPLOAD_STATE_WORDS = ("取消上传", "重新上传", "上传失败")

    def _has_visible(self, page, key: str, *, max_probe: int = 10) -> bool:
        """选择器键是否命中**至少一个可见**元素。

        ⚠️ 不能用 ``loc.count() > 0``：``text=重新上传`` 在 DOM 里**常驻**
        隐藏模板元素（探针实测 hasRetry 恒真），count 永远 >0，
        会让「上传失败」在投递后 2 秒就误报。
        """
        loc = self._locators(page, key)
        if loc is None:
            return False
        try:
            n = min(loc.count(), max_probe)
        except Exception:
            return False
        for i in range(n):
            try:
                if loc.nth(i).is_visible():
                    return True
            except Exception:
                continue
        return False

    def _upload_in_flight(self, page) -> bool:
        """是否正在上传 —— **唯一判据是「取消上传」按钮可见**。

        它只在上传进行时出现在 UI 浮层里；完成/失败后即消失。
        **绝不能**拿页面里任意 `NN%` 文本当兜底：2026-10-13 实测，
        发表页常年存在 0%/100%/333% 等隐藏模板文本，兜底后
        `_upload_state` 恒为 True → 收敛判定永远不满足 → 看门狗把
        「已成功」误判成「90s 卡死」。
        locator 能穿透 open shadow root，也不受文本长度上限影响
        （`inner_text` 爬取在这里恒为 False，早期因此造成假成功）。
        """
        return self._has_visible(page, "upload_cancel")

    def _upload_failed(self, page) -> bool:
        """平台是否报了上传失败（可见的「重新上传/上传失败」，且不在上传中）。"""
        return self._has_visible(page, "upload_fail") and not self._upload_in_flight(page)

    def _upload_state(self, page) -> bool:
        """兼容旧调用：是否处于「上传浮层可见」状态（上传中或失败态）。"""
        return self._upload_in_flight(page) or self._upload_failed(page)

    def _upload_completed_evidence(self, page) -> bool:
        """「秒传」完成证据：封面预览区（个人主页卡片/分享卡片）已生成。

        这两块文案**只在视频上传/转码完成后**出现（配合视频缩略图与删除钮）。
        实测个别次上传浮层（取消上传）一帧都不渲染、视频却已秒传完成
        （2026-10-14 00:12 潮汐），若只认浮层会误判失败白重试。
        """
        try:
            has_cards = False
            for f in self._frames(page):
                try:
                    a = f.locator("text=个人主页卡片")
                    b = f.locator("text=分享卡片")
                    if a.count() > 0 and b.count() > 0:
                        if a.first.is_visible() and b.first.is_visible():
                            has_cards = True
                            break
                except Exception:
                    continue
            return (has_cards
                    and not self._upload_in_flight(page)
                    and not self._upload_failed(page))
        except Exception:
            return False

    def _upload_video(self, page, item: VideoItem) -> None:
        """投递视频并**确认上传真的完成**才返回。

        分两段，缺一不可（这里踩过两次最贵的坑）：

        1. **bootstrap**：UI 的上传浮层不是立刻出现的（实测投递后 ~5–11s 才渲染）。
           早期实现只判断「描述框在 && 没有『上传中』」，于是投递后 3~6s 就断定
           「上传完成」——而此时浮层根本还没出现，后面平台弹「请上传视频」
           却已经记成 success（**假成功**）。所以必须**先等到浮层出现**。
        2. **收敛**：等「取消上传」消失才算完成。

           ⚠️ 2026-10-13 第二次修正：**「上传中」的判据只有「取消上传」按钮**。
           旧实现把「页面上存在任意 NN% 文本」当上传态兜底，但发表页常年
           存在 0%/100%/333% 等隐藏模板文本 → 收敛永不满足 → 看门狗把
           **已经成功**的上传误判成「90s 进度停在 0%」（假失败）。
           现在百分比只用于打日志，不参与任何状态判定。

        另外全程盯着 ``helper_upload_params``：一旦平台拒发上传凭证，
        立刻以不可重试错误退出（字节永远不会开始动，等下去纯属浪费）。
        """
        loc = self._locate(page, "file_input")
        if loc is None:
            raise UploadError("未找到视频上传输入框（选择器可能已过期）")
        loc.set_input_files(item.video_path)
        log.info("已投递视频文件：%s（等待平台上传/转码）", Path(item.video_path).name)

        deadline = time.time() + self.upload_timeout_ms / 1000
        bootstrap_deadline = time.time() + self.upload_start_timeout_ms / 1000
        seen_uploading = False
        last_log_pct: str | None = None

        while time.time() < deadline:
            page.wait_for_timeout(2000)

            # ① 平台拒发上传凭证 → 立刻、明确地失败
            fail = self._upload_param_failure()
            if fail is not None:
                self._dump_state(page, "upload_refused")
                raise fail

            # ② 平台明确报上传失败
            if self._upload_failed(page):
                self._dump_state(page, "upload_failed")
                raise UploadError(
                    "平台报上传失败（页面出现「重新上传/上传失败」且上传浮层已消失）。",
                    detail={"upload_param_ok": self._upload_param_ok,
                            "diagnosis": "upload_failed_by_platform",
                            "hint": "多为网络抖动或文件不被接受，可重试；"
                                    "若持续失败先跑 `python main.py preflight`。"
                                    "现场已存 state/submit_dumps/upload_failed.*"},
                )

            # ③ 上传中：只记日志，不做状态判定
            if self._upload_in_flight(page):
                if not seen_uploading:
                    seen_uploading = True
                    log.info("检测到上传浮层（取消上传），等待上传/转码完成…")
                pct = self._upload_progress(page)
                cur = ",".join(pct[:3]) if pct else ""
                if cur and cur != last_log_pct:
                    log.debug("上传进度（仅日志，不作判据）：%s", cur)
                    last_log_pct = cur
                continue

            # ④ 上传浮层已消失
            if seen_uploading:
                # 双重确认：等 2s 再查一次，防止浮层渲染抖动
                page.wait_for_timeout(2000)
                if self._upload_in_flight(page):
                    continue
                if self._upload_failed(page):
                    continue  # 交给 ② 下一轮处理
                log.info("视频上传/转码完成（「取消上传」已消失）")
                return

            # ⑤ 还没见过上传浮层：先查「秒传」证据，再等 bootstrap 截止
            if self._upload_completed_evidence(page):
                log.warning("投递后未观察到上传浮层，但封面预览区已生成 —— 判定秒传完成")
                return
            if time.time() > bootstrap_deadline:
                self._dump_state(page, "upload_overlay_never_seen")
                raise UploadError(
                    f"投递后 {int(self.upload_start_timeout_ms / 1000)}s 内从未出现上传浮层，"
                    "也无法确认完成 —— 不再按「已完成」放行（防假成功）。",
                    detail={"upload_param_ok": self._upload_param_ok,
                            "diagnosis": "upload_overlay_never_seen",
                            "hint": "若 preflight 通过但反复如此，可能是上传组件"
                                    "加载失败，可加 -v 复跑并查看控制台日志。"
                                    "现场已存 state/submit_dumps/upload_overlay_never_seen.*"},
                )

        raise UploadError("等待视频上传/转码超时",
                          detail={"timeout_ms": self.upload_timeout_ms,
                                  "seen_uploading": seen_uploading,
                                  "upload_param_ok": self._upload_param_ok})

    def _close_activity_dropdown(self, page) -> bool:
        """收起活动下拉，返回是否确认已收起。

        ⚠️ 必须在 `_join_activity` 的**每一条**退出路径上调用：
        下拉不收起时，「搜索活动」输入框会浮在表单上层，
        拦截后续所有点击（实测把「定时」radio 挡到 15s 超时：
        `input intercepts pointer events`）。

        踩坑记录：``page.keyboard.press("Escape")`` 打到**主 frame**，
        而表单（含下拉）在 iframe / shadow DOM 里，根本收不到 ——
        所以必须逐 frame 发 Escape，再用「点外面」兜底，最后**校验**。
        """
        for _ in range(3):
            if not self._has_visible(page, "activity_search"):
                return True
            # ① 逐 frame 发 Escape（表单在 iframe 里，page.keyboard 收不到）
            for f in self._frames(page):
                try:
                    f.locator("body").press("Escape", timeout=1_000)
                except Exception:
                    continue
            page.wait_for_timeout(600)
            if not self._has_visible(page, "activity_search"):
                return True
            # ② 点表单空白处（描述框），让下拉失焦收起
            try:
                box = self._locate(page, "desc_box")
                if box is not None:
                    box.click(timeout=2_000)
                    page.wait_for_timeout(600)
            except Exception:
                pass
            if not self._has_visible(page, "activity_search"):
                return True
            # ③ 再点一次展示区（开→关切换）
            try:
                self._click_first_visible(page, self.selectors["activity_display"])
                page.wait_for_timeout(600)
            except Exception:
                pass
        closed = not self._has_visible(page, "activity_search")
        if not closed:
            log.warning("活动下拉未能确认收起（后续点击可能被搜索框拦截）")
        return closed

    def _activity_display_text(self, page) -> str:
        try:
            disp = self._locate(page, "activity_display")
            return (disp.inner_text() or "").strip() if disp is not None else ""
        except Exception:
            return ""

    def _activity_search_texts(self, page) -> list[str]:
        """当前下拉里的活动项文本快照（最多 30 项）。"""
        items = self._locators_any(page, self.selectors["activity_item"])
        if items is None:
            return []
        out = []
        n = min(items.count(), 30)
        for i in range(n):
            try:
                if items.nth(i).is_visible():
                    out.append((items.nth(i).inner_text() or "").strip().replace("\n", " "))
            except Exception:
                continue
        return out

    def _dismiss_blocking_dialogs(self, page, *, max_rounds: int = 3) -> list[str]:
        """关掉挡在表单上层的平台弹窗，返回关掉的内容摘要。

        实测弹窗（2026-10-13，发布中途触发）：
          * 「为保障账号安全，需用管理员微信扫码完成实名验证后才能继续发表」
            （只有 × 关闭钮）—— 二维码截图会留档；
          * 「扫码前往绑定带货者账号」（我知道了 / 去绑定）。
        这类弹窗会让后续一切点击报 ``intercepts pointer events``。
        只点关闭/知道了类控件，**绝不能点「去绑定」**（会跳走）。
        """
        dismissed: list[str] = []
        for _ in range(max_rounds):
            acted = False
            for key, tag in (("dialog_close", "×"), ("dialog_ok_btn", "我知道了")):
                if self._click_first_visible(page, self.selectors[key], timeout_ms=2_500):
                    dismissed.append(tag)
                    acted = True
                    page.wait_for_timeout(800)
                    break
            if not acted:
                break
        if dismissed:
            log.info("已关闭平台弹窗：%s", " → ".join(dismissed))
            page.screenshot(path=str(self._dump_dir / "dialog_dismissed.png"))
        return dismissed

    def _join_activity(self, page, activity_name: str) -> str | None:
        """在发布表单里把「活动」设为指定活动，返回平台侧选中项全文。

        网页端活动下拉显示为「账号名 · 活动名」（例如
        「白版讲清楚 · 白版说知边看边学」），**不是**手机端的活动大标题
        （「少儿科普·出题挑战」）—— 配置里应填网页端可见的活动名。

        实测要点：
          * 活动列表是异步的，必须先在「搜索活动」框里键入关键词；
            **搜索用短词**（如「白版」）比全名更容易命中（全名常搜不到）；
          * 结果列表会随输入法/防抖持续刷新 —— 必须等**连续两次快照一致**
            再点，否则会点中重渲染前的错位项（实测选成了别人的活动）；
          * 点击后显示区更新有延迟，要轮询回读；回读不符要**回滚到
            「不参与活动」**，绝不能把错的活动留在表单上提交。

        失败策略：告警并放行（回滚到「不参与」），不让参加活动失败拖垮发布。
        """
        try:
            return self._join_activity_inner(page, activity_name)
        finally:
            self._close_activity_dropdown(page)

    def _join_activity_inner(self, page, activity_name: str) -> str | None:
        content_cfg = self.config.content
        keyword = (getattr(content_cfg, "activity_keyword", "") or activity_name)
        # 搜索关键词逐级放宽：短词 → 全名。实测短词更易命中，但结果偶尔不稳。
        keywords = [keyword] + [k for k in (activity_name, activity_name[:3]) if k and k != keyword]
        disp = self._locate(page, "activity_display")
        if disp is None:
            log.warning("页面上没有「活动」入口，跳过参加活动")
            return None
        cur = self._activity_display_text(page)
        if activity_name in cur:
            log.info("活动已是「%s」，无需重复设置", cur)
            return cur

        if not self._click_first_visible(page, self.selectors["activity_display"]):
            log.warning("未能点开活动下拉，跳过参加活动")
            return None
        page.wait_for_timeout(800)

        for attempt, kw in enumerate(keywords, start=1):  # 每轮换一个搜索关键词
            sinp = self._locate(page, "activity_search")
            if sinp is None:
                log.warning("未找到「搜索活动」输入框，跳过参加活动")
                return None
            try:
                sinp.click()
                sinp.fill("")
                sinp.type(kw, delay=80)
            except Exception as exc:  # noqa: BLE001
                log.warning("活动搜索框输入失败（%s），跳过参加活动", exc)
                return None

            # 等结果列表**连续两次快照一致**（防抖/重渲染错位）
            prev: list[str] | None = None
            snapshot: list[str] = []
            for _ in range(16):
                page.wait_for_timeout(500)
                snapshot = self._activity_search_texts(page)
                if snapshot and snapshot == prev:
                    break
                prev = list(snapshot)
            if snapshot:
                log.info("活动搜索（%s）返回 %d 项：%s", kw, len(snapshot),
                         " | ".join(snapshot[:5]))

            matched_idx = None
            items = self._locators_any(page, self.selectors["activity_item"])
            if items is not None:
                n = min(items.count(), 30)
                for i in range(n):
                    try:
                        if not items.nth(i).is_visible():
                            continue
                        txt = (items.nth(i).inner_text() or "").strip().replace("\n", " ")
                    except Exception:
                        continue
                    if "不参与活动" in txt:
                        continue
                    if activity_name in txt:
                        matched_idx = i
                        break
            if matched_idx is None:
                log.warning("搜索结果中没有匹配「%s」的活动（关键词=%s，第 %d 轮）",
                            activity_name, kw, attempt)
                continue

            try:
                items.nth(matched_idx).click(timeout=5_000)
            except Exception as exc:  # noqa: BLE001
                log.warning("点击活动项失败（%s），第 %d 轮", exc, attempt)
                continue

            # 轮询回读（最多 6s）
            got = ""
            for _ in range(12):
                page.wait_for_timeout(500)
                got = self._activity_display_text(page)
                if activity_name in got:
                    log.info("已参加活动：%s", got)
                    return got
            log.warning("活动点击后回读为「%s」，与目标不符（第 %d 轮）", got or "(空)", attempt)
            # 回读不符：把可能选错的活动**回滚到「不参与」**，再试一轮
            self._reset_activity_to_none(page)

        # 两轮都失败：确保最终停在「不参与活动」，别把错的活动留在表单上
        final = self._activity_display_text(page)
        if final and activity_name not in final and "不参与" not in final:
            self._reset_activity_to_none(page)
            log.warning("活动选择失败，已回滚为「不参与活动」")
        log.warning("未能参加活动「%s」，继续发布（不影响视频与定时）", activity_name)
        return None

    def _reset_activity_to_none(self, page) -> None:
        """把「活动」重置回「不参与活动」（防错选别人的活动提交出去）。"""
        try:
            if not self._has_visible(page, "activity_search"):
                self._click_first_visible(page, self.selectors["activity_display"])
                page.wait_for_timeout(800)
            items = self._locators_any(page, self.selectors["activity_item"])
            if items is None:
                return
            n = min(items.count(), 30)
            for i in range(n):
                try:
                    txt = (items.nth(i).inner_text() or "").strip()
                except Exception:
                    continue
                if "不参与活动" in txt and items.nth(i).is_visible():
                    items.nth(i).click(timeout=5_000)
                    page.wait_for_timeout(600)
                    return
        except Exception as exc:  # noqa: BLE001
            log.debug("回滚活动选择失败：%s", exc)

    def _fill_description(self, page, item: VideoItem) -> None:
        loc = self._locate(page, "desc_editor")
        if loc is None:
            raise UploadError("未找到视频描述编辑器")
        parts = [item.title]
        if item.description:
            parts.append(item.description)
        # 话题去重：发布.md 的简介里可能已带 #话题，再无脑追加默认话题会
        # 重复（实测「#白版 #少儿科普」发成了两遍）。已有的一律跳过。
        existing = set(re.findall(r"#([^\s#＃，。；!,;]+)", item.description or ""))
        tags = " ".join(t for t in item.hashtags() if t.lstrip("#") not in existing)
        if tags:
            parts.append(tags)
        text = "\n".join(parts)[: self.config.content.description_max_length]

        loc.click()
        page.wait_for_timeout(400)
        # 用真实键盘事件输入（该编辑器监听 input/keydown，直接改 DOM 不会被框架识别）
        page.keyboard.type(text, delay=12)
        log.info("描述已填入（%d 字）", len(text))

        short = self._locate(page, "short_title")
        if short is not None and item.title:
            try:
                short.click()
                short.fill(item.title[:30])
                log.info("短标题已填入：%s", item.title[:30])
            except Exception as exc:
                log.warning("短标题填写失败（不影响发布）：%s", exc)

    def _wait_button_enabled(self, page, key: str, timeout_s: int = 180):
        """等提交按钮出现且去掉 ``weui-desktop-btn_disabled``。

        上传刚完成时按钮常还是 disabled（平台还在转码），
        此处耐心等而不是直接失败 —— 否则会触发整条重试、白白重传一遍视频。
        """
        deadline = time.time() + max(timeout_s, 0)
        loc = None
        while time.time() < deadline:
            loc = self._locate(page, key)
            if loc is not None:
                try:
                    cls = loc.get_attribute("class") or ""
                except Exception:
                    cls = "disabled"
                if "disabled" not in cls:
                    return loc
            page.wait_for_timeout(3000)
        return loc

    def _upload_cover(self, page, item: VideoItem) -> None:
        """分别给「个人主页卡片 3:4」和「分享卡片 4:3」上传封面。

        ⚠️ 旧实现把图片塞给第一个隐藏 file input 就返回 —— 平台根本没接受，
        两张封面卡片全是空白（2026-10-13 用户实测）。必须点开每张卡片
        的「编辑」弹窗，把图传进**弹窗里可见的** file input，再点确定。

        封面取自视频同目录（用户约定：横竖各一张，直接用）：
          * ``封面-3x4.*``（竖）→ 个人主页卡片 3:4
          * ``封面-4x3.*``（横）→ 分享卡片 4:3（缺省回退 item.cover_path）
        """
        folder = Path(item.cover_path).parent if item.cover_path else None
        vertical = self._find_cover_file(folder, ("3x4", "3：4", "3X4")) if folder else None
        horizontal = self._find_cover_file(folder, ("4x3", "4：3", "4X3")) if folder else None
        horizontal = horizontal or (Path(item.cover_path) if item.cover_path else None)

        # 平台会记住上次发布的位置（实测残留「南京市」）→ 用户要求不带位置
        self._clear_location(page)

        for wrap, img in (("vertical-cover-wrap", vertical),
                          ("horizon-cover-wrap", horizontal)):
            if not img:
                log.warning("缺少封面文件，跳过 %s", wrap)
                continue
            try:
                self._set_cover_card(page, wrap, img)
            except Exception as exc:  # noqa: BLE001
                log.warning("封面 %s 上传失败（不阻断发布）：%s", wrap, exc)

    @staticmethod
    def _find_cover_file(folder: Path, marks: tuple[str, ...]) -> Path | None:
        """在视频同目录里按「3x4 / 4x3」标记找封面图。"""
        if not folder or not folder.exists():
            return None
        for pat in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            for p in sorted(folder.glob(pat)):
                stem = p.stem.upper()
                if any(m.upper() in stem for m in marks):
                    return p
        return None

    def _visible_cover_dialog_text(self, page) -> str:
        """当前可见的封面编辑弹窗文本（没有则返回空串）。

        弹窗标题能区分是哪张卡：「编辑个人主页卡片」= 3:4，「编辑分享卡片」= 4:3。
        ⚠️ 必须 **iframe 优先**：主 frame 的镜像副本里也有一份「可见」的
        旧弹窗（内容停留在上一张卡），先扫它会把 4:3 的真弹窗误判成错卡
        然后 Escape 关掉（2026-10-14 实测 4:3 封面因此三次轮空）。
        """
        frames = [f for f in self._frames(page) if f is not page.main_frame] + [page.main_frame]
        for f in frames:
            try:
                dlgs = f.locator(".weui-desktop-dialog")
                for i in range(min(dlgs.count(), 12)):
                    d = dlgs.nth(i)
                    try:
                        if not d.is_visible():
                            continue
                        txt = (d.inner_text() or "").strip()
                        if ("确定" in txt) or ("确认" in txt):
                            return txt
                    except Exception:
                        continue
            except Exception:
                continue
        return ""

    def _escape_all_frames(self, page) -> None:
        """逐 frame 发 Escape（page.keyboard 打不到 iframe）。"""
        for f in self._frames(page):
            try:
                f.locator("body").press("Escape", timeout=800)
            except Exception:
                continue

    def _scroll_cover_into_view(self, page) -> None:
        """把封面卡片区滚回视口中央。

        实测（2026-10-17）：参加过活动 / 填完描述后，页面（iframe 内）被自动
        滚走，「编辑」按钮 rect 的 y 从 185 变成 -363 —— 坐标已失效，
        mouse.click 落在视口外的空白处，弹窗文本恒为空、三步重试全废。
        滚回视口再收坐标即可恢复。最小流程不触发滚动，所以一直没暴露。
        """
        for f in self._frames(page):
            try:
                f.evaluate("""() => {
                  const el = document.querySelector('.vertical-cover-wrap')
                          || document.querySelector('.horizon-cover-wrap');
                  if (el) el.scrollIntoView({block: 'center', behavior: 'instant'});
                }""")
            except Exception:
                continue

    # 收集所有可见「编辑」按钮：坐标 + 归属卡片文本。
    # 同时走主文档/shadow 与 iframe 副本（同一按钮两份，坐标相同，
    # 点坐标即命中渲染在上面的活副本）；iframe 坐标要叠加 iframe 自身位置。
    _COVER_EDIT_JS = r"""() => {
  const out = [];
  const walk = (root, tag, ox, oy) => {
    let all = [];
    try { all = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of all) {
      if (el.shadowRoot) walk(el.shadowRoot, tag + '/shadow', ox, oy);
      const own = Array.from(el.childNodes)
        .filter(n => n.nodeType === 3)
        .map(n => n.textContent.trim()).join('');
      if (own !== '编辑') continue;
      const r = el.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0)) continue;
      const st = getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden') continue;
      let card = el.closest('[class*=cover], [class*=card], label, .form-item');
      out.push({
        frame: tag,
        rect: [Math.round(r.x + ox), Math.round(r.y + oy),
               Math.round(r.width), Math.round(r.height)],
        cardText: card ? (card.innerText || '').trim().slice(0, 80) : '',
      });
    }
  };
  walk(document, 'main', 0, 0);
  for (const f of document.querySelectorAll('iframe')) {
    try {
      const fr = f.getBoundingClientRect();
      walk(f.contentDocument, 'iframe', fr.x, fr.y);
    } catch (e) {}
  }
  const seen = new Set();
  return out.filter(t => {
    const key = t.rect.join(',') + '|' + t.cardText;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }).sort((a, b) => a.rect[0] - b.rect[0]);
}"""

    def _cover_edit_targets(self, page) -> list:
        """所有可见封面「编辑」按钮（坐标 + 归属卡片文本），按 x 升序。"""
        try:
            return page.evaluate(self._COVER_EDIT_JS) or []
        except Exception as exc:  # noqa: BLE001
            log.warning("收集封面编辑候选失败：%s", exc)
            return []

    def _hover_click_cover_edit(self, page, rect: list) -> str:
        """真实鼠标 hover + 点击封面「编辑」按钮，返回弹窗文本（空=没打开）。

        实测（2026-10-14 ``probe_cover_layout``）：两个「编辑」按钮都带
        ``pointer-events: none``，点击之所以能生效，是因为**先 hover 让卡片
        弹出 img-popover 浮层**（全屏 mask + 可点的编辑入口），再点按钮坐标。
        直接 ``locator.click(force=True)`` 对 4:3 卡无效 —— :hover 尚未生效，
        点击落在浮层出现前的卡片上，什么都不发生（3:4 卡因带
        ``edit-btn-zIndex`` 侥幸能中）。必须 mouse.move + 停 800ms + click。

        ⚠️ 第二张卡开始，平台会拦截点击弹「使用此素材作为封面?」浮层
        （3:4 上传后产生了素材），必须先点浮层里的**直接编辑**才能进真弹窗
        （2026-10-14 截图实测：浮层含「直接编辑」「使用素材」两个按钮；
        点「使用素材」会把 3:4 竖图塞给 4:3 横卡 —— 坚决不点）。
        """
        cx = rect[0] + rect[2] // 2
        cy = rect[1] + rect[3] // 2
        # 坐标失效防护：前置步骤可能把页面滚走（按钮 y 变负），
        # 此时点击落在视口外空白处，弹窗永远为空。滚回去让外层重收坐标。
        try:
            vh = (page.viewport_size or {}).get("height", 900)
        except Exception:
            vh = 900
        if cy < 0 or cy > vh:
            log.warning("封面按钮坐标 %d 在视口外（视口高 %d），先滚回封面区再试", cy, vh)
            self._scroll_cover_into_view(page)
            page.wait_for_timeout(600)
            return ""
        try:
            page.mouse.move(cx, cy)
            page.wait_for_timeout(800)  # 等 :hover 弹层渲染出来
            page.mouse.click(cx, cy)
        except Exception as exc:  # noqa: BLE001
            log.warning("封面编辑点击异常：%s", exc)
            return ""
        # 弹窗可能因网络慢而延迟渲染，轮询等待，别只探一次就判失败
        text = ""
        for _ in range(8):  # 8 × 500ms ≈ 4s
            page.wait_for_timeout(500)
            text = self._visible_cover_dialog_text(page)
            if text:
                return text
        # 兜底：把鼠标移开再移回，强制重新触发 :hover 后再点一次
        #（残留遮罩/浮层会吃掉第一次点击，2026-10-17 实测）
        try:
            page.mouse.move(20, 20)
            page.wait_for_timeout(400)
            page.mouse.move(cx, cy)
            page.wait_for_timeout(900)
            page.mouse.click(cx, cy)
            page.wait_for_timeout(1_500)
            text = self._visible_cover_dialog_text(page)
            if text:
                log.info("二次 hover 点击后封面弹窗已打开")
                return text
        except Exception as exc:  # noqa: BLE001
            log.warning("封面编辑二次点击异常：%s", exc)
        # 平台拦截：「使用此素材作为封面?」→ 点「直接编辑」进真弹窗
        if self._click_first_visible(
                page, ["button:has-text('直接编辑')",
                       ".weui-desktop-btn:has-text('直接编辑')",
                       ":text-is('直接编辑')",
                       "[class*=popover] :text-is('直接编辑')"],
                timeout_ms=3_000):
            log.info("已点封面素材浮层的「直接编辑」")
            page.wait_for_timeout(1_800)
            return self._visible_cover_dialog_text(page)
        return ""

    def _set_cover_card(self, page, wrap: str, img: Path) -> None:
        """给一张封面卡片（3:4 竖 / 4:3 横）走完整编辑弹窗流程。

        实测要点（2026-10-14 凌晨，全部踩过）：
          * 「编辑」按钮随封面预览**异步渲染**，必须轮询等它出现；
          * 按钮 ``pointer-events: none``，必须**先 hover 弹出浮层再点坐标**
            （见 ``_hover_click_cover_edit``）；
          * 按**归属卡片文本**（个人主页卡片/分享卡片）认按钮，不靠 x 猜；
          * 点完**必须验证弹窗标题**（「编辑个人主页卡片」/「编辑分享卡片」），
            不符就 Escape 换下一个候选；
          * 弹窗里点「上传封面」对应隐藏 file input，直接 set_input_files；
          * 最后点「确认」并验证弹窗关闭。
        """
        slot = "3:4" if "vertical" in wrap else "4:3"
        expect_word = "个人主页" if slot == "3:4" else "分享"
        card_mark = "个人主页卡片" if slot == "3:4" else "分享卡片"

        dialog_text = ""
        for attempt in (1, 2, 3):  # 第一张卡编辑完后预览区会重渲染，候选需要重新收集
            # 每轮先清场 + 把封面区滚回视口：前置步骤（活动下拉/位置选择器）
            # 会把页面滚走导致坐标失效；残留浮层或全屏遮罩也会吃掉封面点击
            # （2026-10-17 实测：参加过活动后按钮 y 变负，点击落空、弹窗恒空）
            self._scroll_cover_into_view(page)
            self._escape_all_frames(page)
            page.wait_for_timeout(500)
            targets = self._cover_edit_targets(page)
            if not targets:
                log.warning("封面「编辑」按钮始终未渲染（%s，第 %d 轮）", slot, attempt)
                page.wait_for_timeout(2_000)
                continue
            # 归属卡片文本精确匹配；匹配不到才按 x 猜（3:4 最左 / 4:3 最右）
            mine = [t for t in targets if card_mark in (t.get("cardText") or "")]
            if not mine:
                mine = targets[:1] if slot == "3:4" else targets[-1:]
            log.info("封面编辑候选（第 %d 轮，%s）：%s", attempt, slot,
                     [(t["rect"][0], (t.get("cardText") or "").replace("\n", "/")[:18])
                      for t in targets])
            opened_ok = False
            for t in mine:
                dialog_text = self._hover_click_cover_edit(page, t["rect"])
                log.info("封面点击 x=%d → 弹窗文本=%r",
                         t["rect"][0], (dialog_text or "")[:40])
                if dialog_text and expect_word in dialog_text:
                    opened_ok = True
                    break
                if dialog_text:  # 打开了另一张卡的弹窗 → 关掉换下一个
                    self._escape_all_frames(page)
                    page.wait_for_timeout(600)
                    dialog_text = ""
            if opened_ok:
                break
            page.wait_for_timeout(2_000)  # 等预览区重渲染完再收一轮
        if not dialog_text:
            log.warning("未能点开封面「编辑」（%s），跳过", slot)
            return
        page.wait_for_timeout(800)

        # 弹窗里的图片 file input（隐藏也能 set_input_files）
        target = None
        for f in self._frames(page):
            try:
                inputs = f.locator("input[type=file][accept*='image']")
                if inputs.count() > 0:
                    target = inputs.first
                    break
            except Exception:
                continue
        if target is None:
            log.warning("封面弹窗里没有图片输入框（%s），跳过", slot)
            self._escape_all_frames(page)
            return
        target.set_input_files(str(img))
        page.wait_for_timeout(2_500)  # 等图加载/裁剪预览

        # 点 确认，验证弹窗真的关闭
        confirmed = False
        for _ in range(3):
            clicked = False
            for text in ("确认", "确定"):
                if self._click_first_visible(
                        page, [f".weui-desktop-dialog button:has-text('{text}')",
                               f"button:text-is('{text}')"], timeout_ms=3_000):
                    clicked = True
                    page.wait_for_timeout(1_500)
                    break
            if clicked:
                confirmed = True
            if not self._visible_cover_dialog_text(page):
                break
        if not confirmed:
            log.warning("封面弹窗未找到确认按钮（%s），尝试 Escape 关闭", slot)
        self._escape_all_frames(page)
        page.wait_for_timeout(500)
        log.info("封面已上传（%s）：%s（确认=%s）", slot, img.name, confirmed)

    def _clear_location(self, page) -> None:
        """把「位置」重置为「不显示位置」。

        实测：平台会记住上次发布的位置（残留「南京市」，橙色显示），
        用户明确要求不带位置。下拉里第一项就是「不显示位置」。
        """
        opened = False
        for f in self._frames(page):
            try:
                items = f.locator(".form-item")
                n = min(items.count(), 15)
                for i in range(n):
                    cand = items.nth(i)
                    try:
                        txt = (cand.inner_text() or "").strip()
                    except Exception:
                        continue
                    if not txt.startswith("位置"):
                        continue
                    try:
                        cand.click(timeout=3_000)
                        opened = True
                        break
                    except Exception:
                        continue
                if opened:
                    break
            except Exception:
                continue
        if not opened:
            log.debug("未找到「位置」控件，跳过清除")
            return
        page.wait_for_timeout(1_000)
        if self._click_first_visible(page, [".location-item:has-text('不显示位置')",
                                            "text=不显示位置"], timeout_ms=3_000):
            log.info("位置已重置为「不显示位置」")
            page.wait_for_timeout(500)
        else:
            log.warning("未点到「不显示位置」，位置可能保留原值")
            self._close_activity_dropdown(page)

    def _submit(self, page, item: VideoItem) -> str:
        mode = self.config.publish.publish_mode
        # 平台侧「定时发表」与「保存草稿」互斥（页面明文提示），
        # 所以 scheduled 走「发表」按钮：提交后内容进入平台定时队列，到点在服务器侧发出。
        key = "draft_btn" if mode == "draft" else "publish_btn"
        btn = self._wait_button_enabled(page, key, timeout_s=180)
        if btn is None:
            raise UploadError(f"未找到提交按钮({key})，页面结构可能已改版")
        # 按钮带 weui-desktop-btn_disabled 说明条件未满足（常见：视频还在转码）
        cls = ""
        try:
            cls = btn.get_attribute("class") or ""
        except Exception:
            pass
        if "disabled" in cls:
            raise UploadError(
                "提交按钮持续处于 disabled —— 通常是视频仍在转码，或必填项未完成",
                detail={"mode": mode, "button_class": cls,
                        "scheduled_at": item.meta.get("scheduled_at", "")},
            )

        self._dump_state(page, "submit_before")
        btn.click()
        page.wait_for_timeout(4000)

        # 二次确认弹窗 / 校验提示。必须点**可见**的那个按钮：
        # 隐藏的同文案按钮遍地都是（主 frame shadow DOM 里尤其多），
        # 早期用 _locate_any().first 会命中隐藏元素 → 弹窗点不掉 → 提交静默失败。
        confirmed = False
        for text in ("确认发表", "确认", "确定", "继续发表", "好的", "我知道了"):
            if self._click_first_visible(page, [f"button:has-text('{text}')"], timeout_ms=4_000):
                log.info("已点击二次确认按钮：%s", text)
                confirmed = True
                page.wait_for_timeout(3000)
                break

        page.wait_for_timeout(3000)
        after = self._dump_state(page, "submit_after")

        body = self._page_text(page)
        for kw in ("审核不通过", "发布失败", "内容违规", "涉及违规"):
            if kw in body:
                raise RemoteRejectError(f"平台拒绝发布：{kw}",
                                        detail={"mode": mode, "post_submit": after.get("frames")})

        log.info("已提交（模式=%s，二次确认=%s）：%s", mode, confirmed, item.item_id)
        return f"web-{int(time.time())}"

    # -- 收尾 --------------------------------------------------------------
    def close(self) -> None:
        for obj, method in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, method)()
            except Exception:
                pass
        self._context = self._browser = self._pw = None

    def healthcheck(self) -> dict[str, Any]:
        out: dict[str, Any] = {"backend": self.name, "ok": False, "checks": []}
        try:
            _require_playwright()
            out["checks"].append({"check": "dependency:playwright", "ok": True})
        except DependencyError as exc:
            out["checks"].append({"check": "dependency:playwright", "ok": False, "error": exc.message})
            out["error"] = exc.message
            return out
        out["checks"].append({"check": "storage_state", "ok": self.storage_state.exists(),
                              "path": str(self.storage_state)})
        try:
            self.authenticate()
            out["checks"].append({"check": "login", "ok": True})
            out["ok"] = True
        except AuthError as exc:
            out["checks"].append({"check": "login", "ok": False, "error": exc.message})
        return out
