# -*- coding: utf-8 -*-
"""
Web状态面板数据大屏混入（ScreenMixin）

独立全屏数据大屏（登录用户可访问）：整页与30秒HTMX轮询数据局部。
与WebPanel其余职责拆分组合使用（见webpanel.py）。
"""

import asyncio

from typing import TYPE_CHECKING, Any, Callable, Dict

from aiohttp import web

from ..utils.timeutil import ms_to_str, now_local, now_ms
from .webpanel_common import _add_days, logger


if TYPE_CHECKING:
    from .bindings import BindingStore
    from ..database.service import DatabaseService


class ScreenMixin:
    """Web面板数据大屏混入（登录用户可访问）"""

    # ---- 以下实例属性由组合后的宿主WebPanel.__init__提供（仅类型标注） ----
    db_service: "DatabaseService"
    binding_store: "BindingStore"

    if TYPE_CHECKING:
        # 鉴权能力（AuthMixin提供）
        _session: Callable[..., Any]
        _web_token_ok: Callable[..., Any]
        _current_user: Callable[..., Any]
        _auth_token_to_admin: Callable[..., Any]
        # 数据收集与渲染能力（PagesMixin提供）
        _collect: Callable[..., Any]
        _streamer_name: Callable[..., Any]
        _render: Callable[..., Any]
        _html_response: Callable[..., Any]

    # ========== 数据大屏（登录用户可访问） ==========

    def _screen_denied(self, request) -> web.Response:
        """未登录访问大屏：HTMX请求返回HX-Redirect，普通请求跳登录页"""
        if request.headers.get("HX-Request"):
            return web.Response(headers={"HX-Redirect": "/login?err=2"},
                                status=200)
        return web.HTTPFound("/login?err=2")

    async def _handle_screen_page(self, request):
        """大屏整页（独立全屏布局，无导航与页头）"""
        # ?token=快捷令牌→建会话后重定向（与其他页面管线对齐）
        token_resp = await self._auth_token_to_admin(request)
        if token_resp is not None:
            return token_resp
        if self._session(request) is None and not self._web_token_ok(request):
            return self._screen_denied(request)
        ctx = await self._screen_context()
        ctx["current_user"] = self._current_user(request)
        html = self._render("screen.html", **ctx)
        return self._html_response(request, html)

    async def _handle_screen_data(self, request):
        """大屏数据局部（30秒HTMX轮询交换）"""
        if self._session(request) is None and not self._web_token_ok(request):
            return self._screen_denied(request)
        ctx = await self._screen_context()
        html = self._render("partials/screen_data.html", **ctx)
        return self._html_response(request, html)

    async def _screen_context(self) -> dict:
        """大屏数据：核心指标/账号池/绑定统计/近7天趋势/房间今日排行/下播记录"""
        data = await self._collect()
        today = now_local()
        room_ids = []
        try:
            room_ids = await self.db_service.list_room_ids() \
                if self.db_service else []
        except Exception:
            room_ids = []
        # 房间今日排行批量并行：今日汇总一次批量扇出（get_daily_summaries
        # 内部并行查各房间子库），主播名复用PagesMixin._streamer_name
        # （TTL缓存），不再逐房串行3N次查询（大屏30秒轮询放大该负载）
        summaries: Dict[int, dict] = {}
        if room_ids and self.db_service is not None:
            try:
                summaries = await self.db_service.get_daily_summaries(
                    room_ids, day=today)
            except Exception as e:
                logger.warning(f"大屏批量查询每日汇总失败: {e}")

        async def _room_row(rid: int) -> dict:
            # 单房间行：主播名查询失败仅降级为房号占位，不拖垮其余房间行
            zero = {"danmaku_count": 0, "gift_gold": 0,
                    "ship_total": 0, "sc_total": 0}
            try:
                name = await self._streamer_name(rid)
            except Exception as e:
                logger.warning(f"大屏查询主播昵称失败(房间{rid}): {e}")
                name = ""
            s = summaries.get(rid) or zero
            return {
                "room_id": rid, "streamer": name or f"房间{rid}",
                "danmaku": s["danmaku_count"], "gold": s["gift_gold"],
                "ship": s["ship_total"], "sc": s["sc_total"],
            }

        rooms_today = list(await asyncio.gather(
            *(_room_row(rid) for rid in room_ids)))
        rooms_today.sort(key=lambda r: r["gold"] + r["ship"] + r["sc"],
                         reverse=True)
        trend7 = await self.db_service.get_daily_history_range(
            _add_days(today.date(), -6), today.date()) \
            if self.db_service else []
        try:
            group_count = len(await self.db_service.load_groups()) \
                if self.db_service else 0
        except Exception:
            group_count = 0
        monitored_bindings = 0
        if self.binding_store is not None:
            try:
                monitored_bindings = sum(
                    len(self.binding_store.get_monitored_groups(rid))
                    for rid in self.binding_store.get_monitored_room_ids())
            except Exception:
                monitored_bindings = 0
        pool_active = sum(1 for a in data["accounts"] if a["status"] == "active")
        return {
            "bot": data["bot"], "me": data["me"], "app_id": data["app_id"],
            "db_size_mb": data["db_size_mb"],
            "now": ms_to_str(now_ms(), "%Y-%m-%d %H:%M:%S"),
            "pool_active": pool_active, "pool_total": len(data["accounts"]),
            "room_count": len(data["rooms"]),
            "alive_rooms": sum(1 for r in data["rooms"] if r["alive"]),
            "dyn_count": len(data["dynamics"]),
            "dyn_alive": sum(1 for d in data["dynamics"] if d["alive"]),
            "group_count": group_count,
            "monitored_bindings": monitored_bindings,
            "rooms_today": rooms_today,
            "trend7": trend7,
            "recent": data["recent"][:6],
        }
