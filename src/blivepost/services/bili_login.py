# -*- coding: utf-8 -*-
"""
B站扫码登录与Cookie维护（基于Playwright无头浏览器）

参考 huntina6/bilibili-login 的实现思路：
- 无头浏览器打开B站首页，在页面上下文内fetch二维码生成/轮询接口
  （credentials:include），登录成功后访问跳转页、首页、视频页，
  触发页面JS生成完整cookie（buvid3、bili_ticket、_uuid等20+项）——
  仅调用API无法获得这些浏览器端cookie。
- 二维码本地渲染为PNG（qrcode库），通过回调交给调用方展示。
- 二维码失效（86038）自动重新生成，最多3次；已扫未确认（86090）继续等待。
- cookie临近过期时带已有cookie重新载入刷新（访问首页+视频页续期票据）。
- 扫码后与刷新后均通过nav接口验证登录态并获取昵称与UID。

依赖（使用时才导入，缺失时给出明确提示）：
    pip install playwright qrcode[pil]
    playwright install chromium
"""

import asyncio
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, List, Optional, Tuple

import requests

from ..utils.logger import get_logger


logger = get_logger("bili_login")

HOME_PAGE = "https://www.bilibili.com"
VIDEO_PAGE = "https://www.bilibili.com/video/BV1GJ411x7h7"
QRCODE_GENERATE_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QRCODE_POLL_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key="
NAV_API = "https://api.bilibili.com/x/web-interface/nav"

HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "referer": "https://www.bilibili.com/",
}

# 二维码状态码
QR_EXPIRED = 86038
QR_SCANNED = 86090


@dataclass
class LoginResult:
    """登录/刷新结果"""
    ok: bool = False
    timeout: bool = False
    uid: int = 0
    nickname: str = ""
    cookie_string: str = ""
    cookies: List[dict] = field(default_factory=list)
    sessdata: str = ""
    bili_jct: str = ""
    ac_time_value: str = ""
    sessdata_expires_at: Optional[datetime] = None
    error: str = ""


def render_qrcode_png(content: str) -> bytes:
    """
    将二维码内容渲染为PNG图片

    Args:
        content: 二维码内容（B站登录URL）

    Returns:
        bytes: PNG图片数据
    """
    import qrcode

    img = qrcode.make(content)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def verify_nav(cookie_string: str) -> Tuple[bool, int, str]:
    """
    通过nav接口验证cookie登录态

    Args:
        cookie_string: 完整cookie字符串

    Returns:
        Tuple[bool, int, str]: (是否登录, UID, 昵称)
    """
    try:
        resp = requests.get(
            NAV_API,
            headers={**HEADERS, "cookie": cookie_string},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0 and data.get("data", {}).get("isLogin"):
            return True, int(data["data"].get("mid", 0)), str(data["data"].get("uname", ""))
    except Exception as e:
        logger.warning(f"nav验证请求失败: {e}")
    return False, 0, ""


def _extract_cookies(cookies: List[dict]) -> LoginResult:
    """
    从playwright cookie列表提取关键字段

    Args:
        cookies: playwright context.cookies()结果

    Returns:
        LoginResult: 含cookie_string、sessdata、过期时间等
    """
    result = LoginResult(ok=True, cookies=cookies)
    parts = []
    for c in cookies:
        parts.append(f"{c['name']}={c['value']}")
        if c["name"] == "SESSDATA":
            result.sessdata = c["value"]
            expires = c.get("expires")
            if expires and expires > 0:
                result.sessdata_expires_at = datetime.fromtimestamp(expires)
        elif c["name"] == "bili_jct":
            result.bili_jct = c["value"]
        elif c["name"] == "ac_time_value":
            result.ac_time_value = c["value"]
    result.cookie_string = ";".join(parts)
    return result


class BiliLoginService:
    """
    B站扫码登录服务（Playwright无头浏览器）
    """

    def __init__(
        self,
        on_qrcode: Optional[Callable[[bytes], Awaitable[None]]] = None,
        timeout: int = 180,
        headless: bool = True,
    ):
        """
        初始化登录服务

        Args:
            on_qrcode: 二维码图片回调（PNG bytes），用于展示给管理员
            timeout: 单个二维码的扫码等待时长（秒）
            headless: 是否无头模式
        """
        self._on_qrcode = on_qrcode
        self._timeout = timeout
        self._headless = headless

    async def _import_playwright(self):
        """惰性导入playwright，缺失时给出明确提示"""
        try:
            from playwright.async_api import async_playwright
            return async_playwright
        except ImportError:
            raise RuntimeError(
                "未安装playwright，请执行: pip install playwright && playwright install chromium"
            )

    async def login(self) -> LoginResult:
        """
        执行扫码登录

        Returns:
            LoginResult: 登录结果（含完整cookie与账号信息）
        """
        async_playwright = await self._import_playwright()

        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.launch(headless=self._headless)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(HOME_PAGE, wait_until="domcontentloaded")
                return await self._login_loop(page, context)
            finally:
                await browser.close()
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"扫码登录异常: {e}")
            return LoginResult(error=f"登录异常: {e}")
        finally:
            await pw.stop()

    async def _login_loop(self, page, context) -> LoginResult:
        """二维码生成与轮询循环"""
        max_refresh = 3
        deadline_reached = False

        for attempt in range(max_refresh):
            # 页面上下文内fetch生成二维码（携带浏览器环境）
            gen = await page.evaluate(
                """async (api) => {
                    const r = await fetch(api, {credentials: "include"});
                    return await r.json();
                }""",
                QRCODE_GENERATE_API,
            )
            qrcode_key = gen["data"]["qrcode_key"]
            qrcode_url = gen["data"]["url"]

            # 渲染二维码并展示
            if self._on_qrcode is not None:
                try:
                    await self._on_qrcode(render_qrcode_png(qrcode_url))
                except Exception as e:
                    logger.error(f"二维码展示回调失败: {e}")

            logger.info(f"等待扫码 (第{attempt + 1}/{max_refresh}个二维码, {self._timeout}秒内有效)...")

            # 页面上下文内轮询登录状态
            deadline = asyncio.get_event_loop().time() + self._timeout
            while asyncio.get_event_loop().time() < deadline:
                poll = await page.evaluate(
                    """async (api) => {
                        const r = await fetch(api, {credentials: "include"});
                        return await r.json();
                    }""",
                    QRCODE_POLL_API + qrcode_key,
                )
                code = poll.get("data", {}).get("code", -1)

                if code == 0:
                    # 扫码确认成功，访问跳转页+首页+视频页触发完整cookie生成
                    redirect_url = poll.get("data", {}).get("url", HOME_PAGE)
                    await page.goto(redirect_url, wait_until="domcontentloaded")
                    await page.goto(HOME_PAGE, wait_until="networkidle")
                    await page.goto(VIDEO_PAGE, wait_until="load")
                    await page.wait_for_timeout(1500)

                    cookies = await context.cookies()
                    result = _extract_cookies(cookies)

                    ok, uid, uname = verify_nav(result.cookie_string)
                    if not ok:
                        logger.warning("扫码成功但nav验证未通过")
                        return LoginResult(error="扫码成功但登录态验证失败")
                    result.uid = uid
                    result.nickname = uname
                    logger.info(f"扫码登录成功: {uname} (UID:{uid})，"
                                f"共获取{len(cookies)}项cookie")
                    return result

                if code == QR_EXPIRED:
                    logger.info("二维码已失效，重新生成")
                    break  # 跳出轮询，生成新二维码

                # 86090=已扫未确认，其他码继续等
                await asyncio.sleep(1)
            else:
                deadline_reached = True

            if deadline_reached:
                logger.info("扫码超时")
                return LoginResult(timeout=True, error="扫码超时")

        return LoginResult(error="二维码多次失效，请稍后重试")

    async def refresh(self, cookie_string: str) -> LoginResult:
        """
        带已有cookie载入刷新（续期SESSDATA与bili_ticket等票据）

        无头浏览器注入cookie后访问首页与视频页，页面JS会刷新
        浏览器端票据cookie，服务端对SESSDATA做滑动续期。

        Args:
            cookie_string: 现有完整cookie字符串

        Returns:
            LoginResult: 刷新结果（含刷新后的完整cookie）
        """
        async_playwright = await self._import_playwright()

        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.launch(headless=self._headless)
            try:
                context = await browser.new_context()
                await _add_cookies_to_context(context, cookie_string)
                page = await context.new_page()
                await page.goto(HOME_PAGE, wait_until="domcontentloaded")
                await page.wait_for_timeout(1000)
                await page.goto(VIDEO_PAGE, wait_until="load")
                await page.wait_for_timeout(2000)

                cookies = await context.cookies()
                result = _extract_cookies(cookies)

                ok, uid, uname = verify_nav(result.cookie_string)
                if not ok:
                    return LoginResult(error="刷新后nav验证未通过，cookie可能已失效")
                result.uid = uid
                result.nickname = uname
                logger.info(f"账号 {uid}（{uname}）cookie刷新完成，共{len(cookies)}项")
                return result
            finally:
                await browser.close()
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"cookie刷新异常: {e}")
            return LoginResult(error=f"cookie刷新异常: {e}")
        finally:
            await pw.stop()


async def _add_cookies_to_context(context, cookie_string: str) -> None:
    """
    将cookie字符串注入playwright浏览器上下文

    Args:
        context: playwright浏览器上下文
        cookie_string: "k=v; k2=v2"形式的cookie字符串
    """
    cookies = []
    for part in cookie_string.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": ".bilibili.com",
            "path": "/",
        })
    await context.add_cookies(cookies)


async def ensure_playwright_chromium() -> bool:
    """
    确保playwright chromium内核已安装（幂等，已安装时快速跳过）

    程序启动时后台调用，安装wheel后无需手动执行
    `playwright install chromium`。

    Returns:
        bool: 内核是否可用
    """
    import sys

    try:
        __import__('playwright')
    except ImportError:
        logger.warning("未安装playwright，跳过浏览器内核检查（扫码登录不可用）")
        return False

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "playwright", "install", "chromium",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        _, _ = await proc.communicate()
        if proc.returncode == 0:
            logger.info("playwright chromium内核已就绪")
            return True
        logger.error(f"playwright chromium安装失败 (exit={proc.returncode})，"
                     f"请手动执行: playwright install chromium")
        return False
    except Exception as e:
        logger.error(f"playwright chromium安装异常: {e}")
        return False
