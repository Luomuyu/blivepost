# -*- coding: utf-8 -*-
"""
动态取数服务（主进程全局单浏览器）

背景（2026-11-10真机实证）：B站动态feed接口对python requests/curl-cffi一律
请求级风控（412），仅真实浏览器环境正常——动态监控依赖无头Chromium取数。
本服务在主进程内持有**全局唯一**浏览器实例（不论订阅多少UP），经本机
HTTP端点向各UP轮询子进程提供取数能力：

    POST http://127.0.0.1:{port}/dyn-fetch  {"url": "https://api.bilibili.com/..."}
    Header: X-Fetch-Token: <随机token>

设计要点：
- 单浏览器单页面串行导航：并发请求自然排队（asyncio单事件循环），避免
  多页面内存开销；浏览器损坏自动重建（冷启动仍保留上次cookie）
- 端点仅接受 api.bilibili.com 前缀URL；仅绑定127.0.0.1；token进程级随机
- 子进程侧失败自动回退各自requests链（见dynamics._RemoteFetchProxy），
  服务不可用不影响"尽力取数"
"""

import asyncio
import json
import secrets
from typing import Any, Optional

from aiohttp import web

from ..utils.logger import get_logger


logger = get_logger("dyn_fetch")

# 允许代理的目标API前缀（动态feed及可能的分页变体）
_ALLOWED_PREFIX = "https://api.bilibili.com/x/polymer/web-dynamic/"


class _BrowserHolder:
    """主进程内全局唯一无头浏览器（异步API；惰性启动，损坏自动重建）

    双页分离（v1.2.0审查修复）：单页时代prepare（opus渲染+截图最长~75s）
    会把并发的/dyn-fetch（60-90s轮询取数）阻塞在同一把页面锁上，子进程
    40s超时后回退requests链→412→误退避误告警。现取数与渲染各用独立
    页面+独立锁（同一浏览器上下文，共享cookie），互不阻塞。
    """

    def __init__(self):
        self._pw: Any = None
        self._browser: Any = None
        self._ctx: Any = None
        self._page: Any = None       # 取数页（API JSON导航）
        self._ppage: Any = None      # 渲染页（opus页+截图）
        self._cookie_string: str = ""
        self._lock = asyncio.Lock()    # 取数页
        self._plock = asyncio.Lock()   # 渲染页

    def set_cookies(self, cookie_string: str) -> None:
        """更新cookie（账号切换时调用；下次浏览器重建生效）"""
        self._cookie_string = cookie_string or ""

    async def _ensure(self) -> bool:
        if self._page is not None:
            return True
        try:
            import os
            from pathlib import Path
            # chromium内核路径与登录模块一致（运行目录ms-playwright/）
            if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(
                    Path.cwd() / "ms-playwright")
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True)
            ctx = await self._browser.new_context()
            cookies = [{"name": n, "value": v,
                        "domain": ".bilibili.com", "path": "/"}
                       for pair in self._cookie_string.split(";") if "=" in pair
                       for n, _, v in [pair.strip().partition("=")]]
            if cookies:
                await ctx.add_cookies(cookies)
            self._ctx = ctx
            self._page = await ctx.new_page()
            self._ppage = await ctx.new_page()
            logger.info("动态取数浏览器已就绪（全局单实例，取数/渲染双页）")
            return True
        except Exception as e:
            logger.warning(f"动态取数浏览器启动失败: {e}")
            await self.close()
            return False

    async def fetch_json(self, url: str) -> Optional[dict]:
        """页面导航到API URL读正文JSON；失败关闭浏览器待下次重建"""
        async with self._lock:
            if not await self._ensure():
                return None
            try:
                resp = await self._page.goto(url, timeout=30000)
                text = await self._page.evaluate("document.body.innerText")
                data = json.loads(text)
                if data.get("code") != 0:
                    logger.warning(
                        f"动态取数接口异常码 {data.get('code')}"
                        f" (HTTP {resp.status if resp else '?'})")
                    return None
                return data
            except Exception as e:
                logger.warning(f"动态取数浏览器导航失败，将重建: {e}")
                await self.close()
                return None

    async def close(self) -> None:
        # 关上下文即同时关两页；逐级关并各自置None（部分失败不影响后续）
        for attr in ("_ctx", "_browser", "_pw"):
            obj = getattr(self, attr)
            if obj is None:
                continue
            try:
                if attr == "_ctx":
                    await obj.close()
                elif attr == "_browser":
                    await obj.close()
                else:
                    await obj.stop()
            except Exception:
                pass
            setattr(self, attr, None)
        self._page = None
        self._ppage = None


# opus页面正文/标题/图片/头像的DOM提取脚本（在页面上下文执行）
_OPUS_EXTRACT_JS = """
() => {
  const pick = (sels) => {
    for (const s of sels) { const el = document.querySelector(s);
      if (el && el.textContent.trim()) return el.textContent.trim(); }
    return '';
  };
  const author = pick(['.opus-module-author__name', '.up-name', '.author-name']);
  const title = pick(['.opus-module-title', 'h1.title', 'h1']);
  let avatar = '';
  for (const s of ['.opus-module-author__avatar img', '.up-avatar img',
                   '.avatar img', '.opus-dyn-author img']) {
    const el = document.querySelector(s);
    if (el && (el.src || '') && /hdslb\\.com|biliimg/.test(el.src)
        && !/emoji/.test(el.src)) { avatar = el.src; break; }
  }
  let text = '';
  const paras = document.querySelectorAll(
    '.bili-opus-view .opus-module-content p, .bili-opus-view .opus-text-richtext p, ' +
    '.bili-opus-view .opus-module-content, .bili-opus-view .opus-text-richtext');
  if (paras.length) {
    text = [...paras].map(p => p.textContent.trim()).filter(Boolean).join('\\n');
  }
  const images = [...document.querySelectorAll('.bili-opus-view img')]
    .map(i => i.src || '')
    .filter(u => /hdslb\\.com|biliimg/.test(u))
    .filter(u => !/face|avatar|emoji/.test(u));
  return {author, avatar, title, text: text.slice(0, 600),
          images: [...new Set(images)].slice(0, 9)};
}
"""

# 本地图片二进制直传大小上限（群files接口file_data限5M，留余量）
_MAX_DIRECT_BYTES = 4 * 1024 * 1024


class DynFetchService:
    """动态取数HTTP服务（127.0.0.1随机端口+进程级token）

    /dyn-prepare配图决策（2026-11-10用户决策）：PIL本地渲染卡片 →
    opus页.bili-opus-view截图 → 动态原图URL → 无图。本地图片以
    image_b64返回，经群files接口二进制直传投递（无需公网URL）。
    """

    def __init__(self, cookie_string: str = ""):
        self._holder = _BrowserHolder()
        self._holder.set_cookies(cookie_string)
        self._token = secrets.token_urlsafe(24)
        self._runner: Optional[web.AppRunner] = None
        self.port: int = 0
        # prepare全流程锁（渲染→截图期间页面不得被并发取数导航走）
        self._prepare_lock = asyncio.Lock()

    @property
    def token(self) -> str:
        """进程级取数令牌（随DynamicManager传给轮询子进程）"""
        return self._token

    def set_cookies(self, cookie_string: str) -> None:
        self._holder.set_cookies(cookie_string)

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/dyn-fetch", self._handle_fetch)
        app.router.add_post("/dyn-prepare", self._handle_prepare)
        return app

    async def start(self) -> int:
        """启动服务（自动选空闲端口）；返回端口号"""
        self._runner = web.AppRunner(self._build_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        # TCPSite不直接暴露端口——从runner取实际绑定地址
        sockets = self._runner.addresses
        self.port = int(sockets[0][1]) if sockets else 0
        logger.info(f"动态取数服务已启动: http://127.0.0.1:{self.port}/dyn-fetch")
        return self.port

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        await self._holder.close()

    # ---- opus页渲染与配图决策 ----

    async def _render_opus(self, dynamic_id: str) -> Optional[dict]:
        """opus页渲染提取内容（标题/正文/作者/原图URL列表）；失败None。

        使用渲染专用页（_ppage，与取数页分离互不阻塞）；调用方须已持有
        self._holder._plock（渲染与后续截图须在同一持锁区间内完成——
        中途释放会让并发prepare把页面导航走，截图元素不在页面上）。
        """
        url = f"https://www.bilibili.com/opus/{dynamic_id}"
        if not await self._holder._ensure():
            return None
        try:
            await self._holder._ppage.goto(url, timeout=30000)
            await self._holder._ppage.wait_for_selector(
                ".bili-opus-view", timeout=15000)
            return await self._holder._ppage.evaluate(_OPUS_EXTRACT_JS)
        except Exception as e:
            logger.warning(f"opus页渲染失败(动态{dynamic_id}): {e}")
            return None

    async def _screenshot_view(self) -> Optional[bytes]:
        """截图.bili-opus-view元素（渲染失败返回None）；调用前须已导航到
        opus页且持有self._holder._plock"""
        try:
            locator = self._holder._ppage.locator(".bili-opus-view").first
            return await locator.screenshot()
        except Exception as e:
            logger.warning(f"opus截图失败: {e}")
            return None

    @staticmethod
    def _render_card(author: str, title: str, text: str,
                     avatar_url: str = "") -> Optional[bytes]:
        """PIL本地渲染自定义动态卡片（头像/作者/标题/正文）；无字体/PIL时None。

        布局严格按存在的分块排（标题为空不产生空行/空白间距——修复
        2026-11-10反馈"标题为空时两行正文"）；头像圆形裁剪嵌入，拉取
        失败静默跳过。
        """
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            return None
        import os
        font_paths = [
            r"C:\Windows\Fonts\msyh.ttc",              # Windows 微软雅黑
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/System/Library/Fonts/PingFang.ttc",       # macOS
        ]
        name_font = small = None
        for p in font_paths:
            if os.path.exists(p):
                try:
                    name_font = ImageFont.truetype(p, 30)
                    small = ImageFont.truetype(p, 24)
                    break
                except Exception:
                    continue
        if name_font is None:
            return None
        title_font = name_font

        W, PAD = 720, 32

        def wrap(s: str, f, limit: int) -> list:
            lines, cur = [], ""
            for ch in s.replace("\r", ""):
                if ch == "\n":
                    lines.append(cur)
                    cur = ""
                    continue
                cur += ch
                if f.getlength(cur) > limit:
                    lines.append(cur[:-1])
                    cur = ch
            if cur:
                lines.append(cur)
            return lines[:12]

        head = author or "B站动态"
        title_lines = wrap(title, title_font, W - PAD * 2) if title else []
        body_lines = wrap(text, small, W - PAD * 2 - (56 + 20)) if text else []

        # 头像拉取（失败跳过，不阻塞卡片）
        avatar = None
        if avatar_url:
            try:
                import requests as _rq
                import io as _io2
                resp = _rq.get(avatar_url, timeout=5)
                resp.raise_for_status()
                avatar = Image.open(_io2.BytesIO(resp.content)).convert("RGBA")
            except Exception:
                avatar = None

        AV = 56  # 头像直径
        # 高度自底向上计算：页脚 + 正文块 + 标题块 + 头部行
        H = PAD + max(AV, 40) + 16           # 头部行（头像/作者名）
        if title_lines:
            H += len(title_lines) * 42 + 8
        if body_lines:
            if title_lines:
                H += 10                       # 标题与正文间距
            H += len(body_lines) * 34
        H += 24 + 30 + PAD                    # 页脚分隔线+落款+底边距

        img = Image.new("RGB", (W, H), "#ffffff")
        d = ImageDraw.Draw(img)
        # 头部：头像（圆形）+ 作者名垂直居中
        if avatar is not None:
            avatar = avatar.resize((AV, AV))
            mask = Image.new("L", (AV, AV), 0)
            md = ImageDraw.Draw(mask)
            md.ellipse((0, 0, AV, AV), fill=255)
            img.paste(avatar, (PAD, PAD), mask)
            d.text((PAD + AV + 20, PAD + (AV - 36) // 2), head,
                   fill="#0f172a", font=name_font)
        else:
            d.text((PAD, PAD + 4), head, fill="#0f172a", font=name_font)
        y = PAD + max(AV, 40) + 16
        # 标题块（存在才渲染与占位）
        for ln in title_lines:
            d.text((PAD, y), ln, fill="#0f172a", font=title_font)
            y += 42
        if title_lines:
            y += 8
        # 正文块
        if body_lines and title_lines:
            y += 10
        for ln in body_lines:
            d.text((PAD, y), ln, fill="#475569", font=small)
            y += 34
        # 页脚
        d.line((PAD, H - 52, W - PAD, H - 52), fill="#e2e8f0", width=2)
        d.text((PAD, H - 44), "BLivePost · B站动态", fill="#94a3b8", font=small)
        import io as _io
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    async def _handle_prepare(self, request: web.Request) -> web.Response:
        """候选动态内容准备：opus渲染+配图决策（截图→卡片→原图→无图）。

        本地图片以image_b64返回——群files接口支持二进制直传（file_data），
        无需公网URL托管。截图优先（保真美观，2026-11-10用户决策）；截图
        失败/超_MAX_DIRECT_BYTES降级PIL卡片（含作者头像），再降原图URL。
        """
        if request.headers.get("X-Fetch-Token") != self._token:
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            dynamic_id = str(body.get("dynamic_id") or "")
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        if not dynamic_id.isdigit():
            return web.json_response({"error": "bad id"}, status=400)

        import base64
        async with self._prepare_lock:
            # 渲染+截图同一持锁区间（渲染专用页_plock，与取数页互不阻塞）：
            # 防止渲染与截图之间页面被并发prepare导航走
            async with self._holder._plock:
                content = await self._render_opus(dynamic_id) or {}
                # ① opus页.bili-opus-view截图（保真美观，用户决策优先）
                shot = await self._screenshot_view()
            image_url = None
            image_b64 = None
            image_source = "none"
            if shot and len(shot) <= _MAX_DIRECT_BYTES:
                image_b64 = base64.b64encode(shot).decode("ascii")
                image_source = "screenshot"
            if image_b64 is None:
                # ② PIL本地渲染卡片（含作者头像；截图失败/超限时兜底）。
                # 网络拉头像可能较慢，不占页面锁
                card = await asyncio.to_thread(
                    self._render_card,
                    content.get("author") or "",
                    content.get("title") or "",
                    content.get("text") or "",
                    content.get("avatar") or "")
                if card and len(card) <= _MAX_DIRECT_BYTES:
                    image_b64 = base64.b64encode(card).decode("ascii")
                    image_source = "card"
            if image_b64 is None and content.get("images"):
                # ③ 动态原图URL（公网，平台可直接转存——与开播封面同链路）
                image_url = content["images"][0]
                image_source = "original"
        return web.json_response({
            "content": content, "image_url": image_url,
            "image_b64": image_b64, "image_source": image_source})

    async def _handle_fetch(self, request: web.Request) -> web.Response:
        if request.headers.get("X-Fetch-Token") != self._token:
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
            url = str(body.get("url") or "")
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        if not url.startswith(_ALLOWED_PREFIX):
            return web.json_response({"error": "url not allowed"}, status=403)
        data = await self._holder.fetch_json(url)
        if data is None:
            return web.json_response({"error": "fetch failed"}, status=502)
        return web.json_response({"data": data})
