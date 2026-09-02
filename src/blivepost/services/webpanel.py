# -*- coding: utf-8 -*-
"""
Web状态面板（只读，aiohttp + Jinja2 + HTMX实现）

暗色现代界面：状态卡片、机器人详情、收入趋势（区间选择）、监控房间/
动态订阅/账号池/下播记录表格。HTMX局部刷新（每30秒自动刷新摘要，趋势
区间切换无整页刷新）。前端样式经CDN加载Tailwind + HTMX。
默认监听127.0.0.1（仅本机访问），无鉴权——请勿直接暴露到公网。
通过Setting.web_panel开关，web_host/web_port配置监听。
"""

import os
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from aiohttp import web
from jinja2 import Environment, FileSystemLoader

from ..utils.logger import get_logger


if TYPE_CHECKING:
    from .bindings import BindingStore
    from .cookie_pool import CookiePool
    from ..core.manager import MonitorManager
    from ..database.service import DatabaseService


logger = get_logger("webpanel")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=True,
)


class WebPanel:
    """Web状态面板服务"""

    def __init__(
        self,
        cookie_pool: "CookiePool",
        monitor_manager: "MonitorManager",
        dynamic_manager,
        binding_store: "BindingStore",
        db_service: "DatabaseService",
        host: str = "127.0.0.1",
        port: int = 8080,
        token: str = "",
        bot_client=None,
    ):
        self.cookie_pool = cookie_pool
        self.monitor_manager = monitor_manager
        self.dynamic_manager = dynamic_manager
        self.binding_store = binding_store
        self.db_service = db_service
        self.host = host
        self.port = port
        self.token = token
        self.bot_client = bot_client
        self._runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        """启动HTTP服务"""
        app = web.Application()
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/panel/summary", self._handle_summary)
        app.router.add_get("/panel/trend", self._handle_trend)
        app.router.add_get("/api/status", self._handle_api)
        app.router.add_get("/api/history", self._handle_history)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info(f"Web面板已启动: http://{self.host}:{self.port}/")

    async def stop(self) -> None:
        """停止HTTP服务"""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def _check_token(self, request) -> bool:
        """访问令牌校验（未配置token时不鉴权；支持query/头/cookie三种携带）"""
        if not self.token:
            return True
        provided = (request.query.get("token")
                    or request.headers.get("X-Web-Token")
                    or request.cookies.get("web_token")
                    or "")
        return provided == self.token

    def _html_response(self, request, html: str) -> web.Response:
        """HTML响应；经query/头鉴权成功时下发cookie供HTMX局部请求携带"""
        resp = web.Response(text=html, content_type="text/html", charset="utf-8")
        if self.token and (request.query.get("token")
                           or request.headers.get("X-Web-Token")) == self.token:
            resp.set_cookie("web_token", self.token, httponly=True, samesite="Lax")
        return resp

    def _render(self, template: str, **ctx) -> str:
        return _env.get_template(template).render(**ctx)

    # ========== 数据收集 ==========

    async def _collect(self) -> dict:
        """收集面板数据"""
        now = datetime.now()

        # 机器人凭证状态（access_token，botpy请求级自动刷新）
        token_state = self.bot_client.token_state() if self.bot_client else {
            "client_started": False, "token_ready": False, "token_expires_in": 0}
        # 机器人详情（username/avatar/share_url，获取失败缓存为{}下轮重试）
        me = await self.bot_client.get_me() if self.bot_client else None
        me = me or {}
        app_id = getattr(self.bot_client, "app_id", "") if self.bot_client else ""

        # 数据库体积（永久保留策略下的增长监控）
        db_size_mb = 0.0
        db_path = getattr(self.db_service, "db_path", None) or getattr(
            getattr(self.db_service, "db_manager", None), "db_path", None)
        if db_path and os.path.exists(db_path):
            db_size_mb = round(os.path.getsize(db_path) / (1024 * 1024), 2)

        accounts = []
        for a in self.cookie_pool.accounts:
            rooms = self.monitor_manager.get_account_rooms(a.uid) if self.monitor_manager else []
            days_left = None
            if a.sessdata_expires_at is not None:
                days_left = max((a.sessdata_expires_at - now).days, 0)
            accounts.append({
                "uid": a.uid, "nickname": a.nickname, "status": a.status,
                "days_left": days_left,
                "use_count": getattr(a, "use_count", 0),
                "expires": a.sessdata_expires_at.strftime("%m-%d") if a.sessdata_expires_at else None,
                "last_used": a.last_used_at.strftime("%m-%d %H:%M") if a.last_used_at else None,
                "rooms": rooms,
            })

        rooms = []
        for rid in self.monitor_manager.room_ids() if self.monitor_manager else []:
            s = await self.db_service.get_daily_summary(rid, day=now)
            rooms.append({
                "room_id": rid,
                "alive": self.monitor_manager.has(rid),
                "uid": self.monitor_manager.get_cookie_uid(rid),
                "streamer": await self._streamer_name(rid),
                "danmaku": s["danmaku_count"],
                "gold": s["gift_gold"], "ship": s["ship_total"], "sc": s["sc_total"],
            })

        dynamics = []
        for uid in self.binding_store.get_dynamic_up_uids():
            dynamics.append({
                "up_uid": uid,
                "name": self.binding_store.get_dynamic_up_name(uid),
                "alive": self.dynamic_manager.has(uid) if self.dynamic_manager else False,
                "groups": len(self.binding_store.get_dynamic_groups(uid)),
            })

        recent = await self.db_service.get_recent_live_status(limit=10)
        return {
            "now": now.strftime("%Y-%m-%d %H:%M:%S"),
            "bot": token_state,
            "me": me,
            "app_id": app_id,
            "db_size_mb": db_size_mb,
            "accounts": accounts,
            "rooms": rooms,
            "dynamics": dynamics,
            "recent": recent,
        }

    async def _streamer_name(self, room_id: int) -> str:
        """直播间对应主播昵称（直接读库：绑定时写入、每日0点批量刷新）"""
        uid = 0
        if self.binding_store is not None:
            for group in self.binding_store.get_monitored_groups(room_id):
                b = self.binding_store.get_binding(group, room_id)
                if b is not None and b.uid:
                    uid = b.uid
                    break
        if not uid or self.db_service is None:
            return ""
        return await self.db_service.get_streamer_name(uid)

    # ========== 页面与局部渲染 ==========

    async def _summary_context(self) -> dict:
        data = await self._collect()
        return {
            "bot": data["bot"],
            "me": data["me"],
            "app_id": data["app_id"],
            "db_size_mb": data["db_size_mb"],
            "accounts": data["accounts"],
            "rooms": data["rooms"],
            "dynamics": data["dynamics"],
            "recent": data["recent"],
            "pool_active": sum(1 for a in data["accounts"] if a["status"] == "active"),
            "pool_total": len(data["accounts"]),
        }

    async def _handle_index(self, request):
        if not self._check_token(request):
            return web.Response(status=401, text="unauthorized: token required")
        summary_ctx = await self._summary_context()
        trend_ctx = await self._trend_context("d30", "")
        html = self._render("index.html", **summary_ctx, **trend_ctx)
        return self._html_response(request, html)

    async def _handle_summary(self, request):
        if not self._check_token(request):
            return web.Response(status=401, text="unauthorized: token required")
        html = self._render("partials/summary.html", **await self._summary_context())
        return self._html_response(request, html)

    async def _handle_trend(self, request):
        if not self._check_token(request):
            return web.Response(status=401, text="unauthorized: token required")
        ctx = await self._trend_context(
            request.query.get("range", "d30"), request.query.get("month", ""))
        html = self._render("partials/trend.html", **ctx)
        return self._html_response(request, html)

    async def _trend_context(self, range_key: str, month_str: str) -> dict:
        """
        趋势数据上下文：近N天 / 本月 / 上月 / 指定自然月 / 近12个月（按月汇总）
        """
        today = datetime.now().date()
        rows: list = []
        mode = "daily"
        active = "d30"
        label = "近30天"
        month_value = today.strftime("%Y-%m")

        if range_key in ("d7", "d14", "d30"):
            n = int(range_key[1:])
            active = range_key
            label = f"近{n}天"
            rows = await self.db_service.get_daily_history_range(
                _add_days(today, -(n - 1)), today)
        elif range_key == "this_month":
            active = range_key
            label = f"{today.year}年{today.month}月"
            rows = await self.db_service.get_daily_history_range(
                today.replace(day=1), today)
        elif range_key == "last_month":
            first_this = today.replace(day=1)
            last_prev = _add_days(first_this, -1)
            start = last_prev.replace(day=1)
            active = range_key
            label = f"{last_prev.year}年{last_prev.month}月"
            rows = await self.db_service.get_daily_history_range(start, last_prev)
        elif range_key == "12m":
            active = range_key
            mode = "monthly"
            label = "近12个月（按月汇总）"
            first_this = today.replace(day=1)
            start = _add_days(first_this, -1).replace(day=1)
            for _ in range(10):
                start = _add_days(start, -1).replace(day=1)
            daily = await self.db_service.get_daily_history_range(start, today)
            rows = _aggregate_monthly(daily)
        elif range_key == "custom_month" and _parse_month(month_str):
            y, m = _parse_month(month_str)
            start = date(y, m, 1)
            if (y, m) == (today.year, today.month):
                end = today
            elif start > today:
                return await self._trend_context("d30", "")  # 未来月份回落近30天
            else:
                next_month = date(y + (m == 12), (m % 12) + 1, 1)
                end = _add_days(next_month, -1)
            active = f"month:{month_str}"
            month_value = month_str
            label = f"{y}年{m}月"
            rows = await self.db_service.get_daily_history_range(start, end)
        else:
            rows = await self.db_service.get_daily_history_range(
                _add_days(today, -29), today)

        return {
            "data": rows, "mode": mode, "active": active, "label": label,
            "month_value": month_value,
            "this_month_key": today.strftime("%Y-%m"),
        }

    # ========== JSON接口 ==========

    async def _handle_history(self, request):
        if not self._check_token(request):
            return web.Response(status=401, text="unauthorized: token required")
        try:
            days = max(min(int(request.query.get("days", 30)), 365), 1)
        except ValueError:
            days = 30
        history = await self.db_service.get_daily_history(days=days)
        return web.json_response({"days": days, "history": history})

    async def _handle_api(self, request):
        if not self._check_token(request):
            return web.Response(status=401, text="unauthorized: token required")
        data = await self._collect()
        return web.json_response(data)


def _add_days(d: date, days: int) -> date:
    from datetime import timedelta
    return d + timedelta(days=days)


def _parse_month(text: str):
    """解析YYYY-MM，返回(y, m)；非法返回None"""
    try:
        parts = text.strip().split("-")
        if len(parts) != 2:
            return None
        y, m = int(parts[0]), int(parts[1])
        if not (1 <= m <= 12) or not (2000 <= y <= 2100):
            return None
        return y, m
    except (ValueError, AttributeError):
        return None


def _aggregate_monthly(daily: list) -> list:
    """按日行汇总为自然月行（day为YYYY-MM），DB无关的纯Python聚合"""
    months: dict = {}
    for row in daily:
        key = row["day"][:7]
        m = months.setdefault(key, {"day": key, "danmaku": 0, "gift_gold": 0.0,
                                    "ship_gold": 0.0, "sc_total": 0})
        m["danmaku"] += row.get("danmaku", 0) or 0
        m["gift_gold"] += row.get("gift_gold", 0) or 0
        m["ship_gold"] += row.get("ship_gold", 0) or 0
        m["sc_total"] += row.get("sc_total", 0) or 0
    for m in months.values():
        m["gift_gold"] = round(m["gift_gold"], 1)
        m["ship_gold"] = round(m["ship_gold"], 1)
    return [months[k] for k in sorted(months)]
