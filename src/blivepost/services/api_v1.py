# -*- coding: utf-8 -*-
"""
REST API v1（只读，JSON）

端点：
  GET /api/v1/rooms                房间列表（含监控状态/今日汇总）
  GET /api/v1/summary?room_id=     房间汇总（默认全部，day参数可选）
  GET /api/v1/trend?days=30        趋势（日弹幕/收入）
  GET /api/v1/analytics?type=      观众分析（heatmap/speakers/distribution）
  GET /api/v1/wordcloud?limit=100  词云数据

鉴权：
  Setting.web_api_public=N（默认）：仅本机/私网可访问（127.0.0.1/10.x/172.16-31.x/192.168.x）
  Setting.web_api_public=Y：对外开放，强制 X-API-Token 头（Setting.web_api_token，
  区别于web_token，可独立吊销）+ 限速60req/min/IP

接入方式：WebPanel.start()路由注册时挂载本模块的register_routes()。
"""

import time
from typing import Any

from aiohttp import web

from ..utils.logger import get_logger


logger = get_logger("api_v1")

# 限速：60次/分钟/IP
_RATE_LIMIT = 60
_RATE_WINDOW = 60.0


class ApiV1Mixin:
    """REST API v1只读端点（挂载到WebPanel）"""

    if True:
        db_service: Any  # 类型由WebPanel组合时确定，mypy经Any绕行
        monitor_manager: Any

    # 限速计数：IP -> (窗口起点monotonic, 计数)
    _api_rate: dict = {}

    def _api_authorized(self, request) -> bool:
        """API鉴权：私网模式仅本机/私网；公网模式强制token"""
        api_public = getattr(getattr(self, 'message_settings', None),
                              'web_api_public', 'N') == 'Y'
        if not api_public:
            return self._is_private_ip(request.remote or '')
        # 公网：强制X-API-Token
        token = getattr(getattr(self, 'message_settings', None),
                        'web_api_token', '')
        if not token:
            logger.warning("API公网模式但web_api_token未配置，拒绝请求")
            return False
        provided = request.headers.get("X-API-Token", "")
        return bool(provided) and provided == token

    @staticmethod
    def _is_private_ip(ip: str) -> bool:
        """是否本机/私网地址"""
        if not ip:
            return False
        if ip.startswith(("127.", "::1", "localhost")):
            return True
        parts = ip.split(".")
        if len(parts) == 4:
            try:
                a, b = int(parts[0]), int(parts[1])
                if a == 10:
                    return True
                if a == 172 and 16 <= b <= 31:
                    return True
                if a == 192 and b == 168:
                    return True
            except ValueError:
                pass
        return False

    def _api_rate_ok(self, request) -> bool:
        """简单IP限速：每_RATE_WINDOW秒_RATE_LIMIT次"""
        ip = request.remote or "unknown"
        now = time.monotonic()
        rec = self._api_rate.get(ip)
        if rec is None or now - rec[0] > _RATE_WINDOW:
            self._api_rate[ip] = (now, 1)
            return True
        if rec[1] >= _RATE_LIMIT:
            return False
        self._api_rate[ip] = (rec[0], rec[1] + 1)
        return True

    async def _api_v1_guard(self, request) -> object:
        """API v1统一守卫：鉴权+限速；通过返回None（否则Response）"""
        if not self._api_authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        if not self._api_rate_ok(request):
            return web.json_response({"error": "rate limit exceeded"}, status=429)
        return None

    async def _handle_api_v1_rooms(self, request):
        denied = await self._api_v1_guard(request)
        if denied:
            return denied
        if self.db_service is None:
            return web.json_response({"error": "db unavailable"}, status=503)
        rooms = []
        for rec in await self.db_service.list_rooms():
            rid = rec.room_id
            s = await self.db_service.get_daily_summary(rid)
            rooms.append({
                "room_id": rid, "uid": rec.uid,
                "monitored": self.monitor_manager.has(rid)
                if self.monitor_manager else False,
                "danmaku_today": s["danmaku_count"],
                "gift_gold_today": s["gift_gold"],
            })
        return web.json_response({"rooms": rooms, "count": len(rooms)})

    async def _handle_api_v1_summary(self, request):
        denied = await self._api_v1_guard(request)
        if denied:
            return denied
        if self.db_service is None:
            return web.json_response({"error": "db unavailable"}, status=503)
        room_id = int(request.query.get("room_id", "0") or 0)
        summaries = await self.db_service.get_daily_summaries(
            [room_id] if room_id else
            (self.db_service.list_room_ids() if hasattr(self.db_service, 'list_room_ids') else []))
        return web.json_response({"summaries": summaries})

    async def _handle_api_v1_trend(self, request):
        denied = await self._api_v1_guard(request)
        if denied:
            return denied
        if self.db_service is None:
            return web.json_response({"error": "db unavailable"}, status=503)
        try:
            days = max(min(int(request.query.get("days", "30")), 365), 1)
        except ValueError:
            days = 30
        history = await self.db_service.get_daily_history(days=days)
        return web.json_response({"days": days, "history": history})

    async def _handle_api_v1_analytics(self, request):
        denied = await self._api_v1_guard(request)
        if denied:
            return denied
        if self.db_service is None:
            return web.json_response({"error": "db unavailable"}, status=503)
        atype = request.query.get("type", "heatmap")
        room_id = int(request.query.get("room_id", "0") or 0)
        if atype == "heatmap":
            data = await self.db_service.get_activity_heatmap(
                room_id=room_id, days=7)
        elif atype == "speakers":
            data = await self.db_service.get_top_speakers(
                room_id=room_id, days=7, limit=20)
        elif atype == "distribution":
            data = await self.db_service.get_danmaku_daily_distribution(
                room_id=room_id, days=30)
        else:
            return web.json_response({"error": "unknown type"}, status=400)
        return web.json_response({"type": atype, "data": data})

    async def _handle_api_v1_wordcloud(self, request):
        denied = await self._api_v1_guard(request)
        if denied:
            return denied
        if self.db_service is None:
            return web.json_response({"error": "db unavailable"}, status=503)
        from .wordcloud import build_wordcloud
        try:
            limit = max(min(int(request.query.get("limit", "100")), 200), 10)
        except ValueError:
            limit = 100
        texts = await self.db_service.get_danmaku_texts()
        words = build_wordcloud(texts, top_n=limit)
        return web.json_response({"words": words, "limit": limit})


def register_api_v1_routes(app: web.Application, panel) -> None:
    """注册API v1路由（WebPanel.start()调用）"""
    app.router.add_get("/api/v1/rooms", panel._handle_api_v1_rooms)
    app.router.add_get("/api/v1/summary", panel._handle_api_v1_summary)
    app.router.add_get("/api/v1/trend", panel._handle_api_v1_trend)
    app.router.add_get("/api/v1/analytics", panel._handle_api_v1_analytics)
    app.router.add_get("/api/v1/wordcloud", panel._handle_api_v1_wordcloud)
    logger.info("REST API v1已注册: /api/v1/rooms|summary|trend|analytics|wordcloud")
