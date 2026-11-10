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
    """主进程内全局唯一无头浏览器（异步API；惰性启动，损坏自动重建）"""

    def __init__(self):
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._cookie_string: str = ""
        self._lock = asyncio.Lock()

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
            self._page = await ctx.new_page()
            logger.info("动态取数浏览器已就绪（全局单实例）")
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
        for attr in ("_page", "_browser", "_pw"):
            obj = getattr(self, attr)
            if obj is None:
                continue
            try:
                if attr == "_page":
                    await obj.context.close()
                elif attr == "_browser":
                    await obj.close()
                else:
                    await obj.stop()
            except Exception:
                pass
            setattr(self, attr, None)


class DynFetchService:
    """动态取数HTTP服务（127.0.0.1随机端口+进程级token）"""

    def __init__(self, cookie_string: str = ""):
        self._holder = _BrowserHolder()
        self._holder.set_cookies(cookie_string)
        self._token = secrets.token_urlsafe(24)
        self._runner: Optional[web.AppRunner] = None
        self.port: int = 0

    @property
    def token(self) -> str:
        """进程级取数令牌（随DynamicManager传给轮询子进程）"""
        return self._token

    def set_cookies(self, cookie_string: str) -> None:
        self._holder.set_cookies(cookie_string)

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/dyn-fetch", self._handle_fetch)
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
