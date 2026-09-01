# -*- coding: utf-8 -*-
"""
Web状态面板（只读，aiohttp实现）

展示账号池状态、监控房间与进程状态、各房间当日汇总、最近下播记录。
默认监听127.0.0.1（仅本机访问），无鉴权——请勿直接暴露到公网。
通过Setting.web_panel开关，web_host/web_port配置监听。
"""

from datetime import datetime
from typing import TYPE_CHECKING, Optional

import os

from aiohttp import web

from ..utils.logger import get_logger


if TYPE_CHECKING:
    from .bindings import BindingStore
    from .cookie_pool import CookiePool
    from ..core.manager import MonitorManager
    from ..database.service import DatabaseService


logger = get_logger("webpanel")

_PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>BLivePost 状态面板</title>
<style>
body{{font-family:"Microsoft YaHei",sans-serif;margin:24px;background:#f7f8fa;color:#333}}
h1{{font-size:20px}} h2{{font-size:16px;margin-top:24px;border-left:4px solid #4a90d9;padding-left:8px}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px}}
th,td{{border:1px solid #e0e0e0;padding:6px 10px;text-align:left}}
th{{background:#f0f4f8}}
.ok{{color:#2e8b57}} .bad{{color:#c0392b}} small{{color:#888}}
</style></head><body>
<h1>BLivePost 状态面板 <small>刷新时间 {now}</small></h1>
<p>机器人凭证：<b>{bot_state}</b>（token剩余 {token_expires_in} 秒）
　数据库体积：<b>{db_size_mb} MB</b> <small>（永久保留策略，数据不自动删除）</small></p>
<h2>账号池（可用 {active}/{total}）</h2>
<table><tr><th>UID</th><th>昵称</th><th>状态</th><th>剩余有效期</th><th>使用次数</th><th>最近使用</th><th>关联房间</th></tr>
{accounts}</table>
<h2>收入趋势（近{hist_days}天） <small><a href="/api/history?days={hist_days}">JSON</a></small></h2>
{trend_svg}
<p><small>图例：蓝=弹幕 橙=礼物(元) 绿=上舰(元) 红=SC(元)
JSON: <a href="/api/history?days={hist_days}">/api/history</a></small></p>
<h2>监控房间（{room_count}）</h2>
<table><tr><th>房间</th><th>进程</th><th>账号</th><th>今日弹幕</th><th>礼物(元)</th><th>上舰(元)</th><th>SC(元)</th></tr>
{rooms}</table>
<h2>动态订阅（{dyn_count}）</h2>
<table><tr><th>UP主UID</th><th>昵称</th><th>轮询进程</th><th>订阅群数</th></tr>
{dynamics}</table>
<h2>最近下播记录</h2>
<table><tr><th>时间</th><th>房间</th><th>弹幕</th><th>礼物(元)</th><th>上舰(元)</th><th>SC(元)</th><th>时长</th></tr>
{live_records}</table>
</body></html>"""


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
        app.router.add_get("/", self._handle_home)
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
        """访问令牌校验（未配置token时不鉴权）"""
        if not self.token:
            return True
        provided = (request.query.get("token")
                    or request.headers.get("X-Web-Token") or "")
        return provided == self.token

    async def _collect(self) -> dict:
        """收集面板数据"""
        now = datetime.now()

        # 机器人凭证状态（access_token，botpy请求级自动刷新）
        token_state = self.bot_client.token_state() if self.bot_client else {
            "client_started": False, "token_ready": False, "token_expires_in": 0}

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
            "db_size_mb": db_size_mb,
            "accounts": accounts,
            "rooms": rooms,
            "dynamics": dynamics,
            "recent": recent,
        }

    async def _handle_history(self, request):
        if not self._check_token(request):
            return web.Response(status=401, text="unauthorized: token required")
        try:
            days = max(min(int(request.query.get("days", 30)), 365), 1)
        except ValueError:
            days = 30
        history = await self.db_service.get_daily_history(days=days)
        return web.json_response({"days": days, "history": history})

    async def _handle_home(self, request):
        if not self._check_token(request):
            return web.Response(status=401, text="unauthorized: token required")
        data = await self._collect()
        rooms = "".join(
            f"<tr><td>{r['room_id']}</td>"
            f"<td class=\"{'ok' if r['alive'] else 'bad'}\">{'运行中' if r['alive'] else '未运行'}</td>"
            f"<td>{r['uid'] or '-'}</td><td>{r['danmaku']}</td>"
            f"<td>{r['gold']}</td><td>{r['ship']}</td><td>{r['sc']}</td></tr>"
            for r in data["rooms"])
        dynamics = "".join(
            f"<tr><td>{d['up_uid']}</td><td>{d['name'] or '-'}</td>"
            f"<td class=\"{'ok' if d['alive'] else 'bad'}\">{'轮询中' if d['alive'] else '未运行'}</td>"
            f"<td>{d['groups']}</td></tr>"
            for d in data["dynamics"])
        recent = "".join(
            f"<tr><td>{r['end_time'].strftime('%m-%d %H:%M') if r.get('end_time') else '-'}</td>"
            f"<td>{r['room_id']}</td><td>{r['danmaku_count']}</td><td>{r['gold_count']}</td>"
            f"<td>{r['ship_count']}</td><td>{r['sc_count']}</td>"
            f"<td>{(r['duration'] or 0) // 3600}h{(r['duration'] or 0) % 3600 // 60}m</td></tr>"
            for r in data["recent"])
        accounts = "".join(
            f"<tr><td>{a['uid']}</td><td>{a['nickname']}</td>"
            f"<td class=\"{'ok' if a['status'] == 'active' else 'bad'}\">{a['status']}</td>"
            f"<td>{('-' if a['days_left'] is None else str(a['days_left']) + '天')}</td>"
            f"<td>{a['use_count']}</td>"
            f"<td>{a['last_used'] or '-'}</td>"
            f"<td>{'、'.join(map(str, a['rooms'])) or '-'}</td></tr>"
            for a in data["accounts"])
        history = await self.db_service.get_daily_history(days=30)
        trend_svg = _render_trend_svg(history)
        bot = data["bot"]
        html = _PAGE.format(
            now=data["now"],
            bot_ready=bot["token_ready"],
            bot_state=("在线" if bot["client_started"] else "离线"),
            token_expires_in=bot["token_expires_in"],
            db_size_mb=data["db_size_mb"],
            active=sum(1 for a in data["accounts"] if a["status"] == "active"),
            total=len(data["accounts"]), accounts=accounts or "<tr><td colspan=7>空</td></tr>",
            hist_days=30, trend_svg=trend_svg,
            room_count=len(data["rooms"]), rooms=rooms or "<tr><td colspan=7>无</td></tr>",
            dyn_count=len(data["dynamics"]),
            dynamics=dynamics or "<tr><td colspan=4>无</td></tr>",
            live_records=recent or "<tr><td colspan=7>无</td></tr>",
        )
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def _handle_api(self, request):
        data = await self._collect()
        return web.json_response(data)


def _render_trend_svg(history: list, width: int = 900, height: int = 220) -> str:
    """
    将按日历史数据渲染为内联SVG折线图（无外部JS依赖）

    Args:
        history: get_daily_history返回的按日数据
        width/height: 画布尺寸

    Returns:
        str: SVG标记
    """
    if not history:
        return "<p><small>暂无趋势数据</small></p>"

    pad_l, pad_r, pad_t, pad_b = 40, 12, 12, 24
    w = width - pad_l - pad_r
    h = height - pad_t - pad_b
    n = len(history)

    def series(key):
        return [row.get(key, 0) for row in history]

    danmaku = series("danmaku")
    gift = series("gift_gold")
    ship = series("ship_gold")
    sc = series("sc_total")

    peak = max(danmaku + gift + ship + sc + [1])
    scale = peak if peak > 0 else 1

    def points(values):
        step = w / max(n - 1, 1)
        pts = []
        for i, v in enumerate(values):
            x = pad_l + i * step
            y = pad_t + h - (v / scale) * h
            pts.append(f"{x:.1f},{y:.1f}")
        return " ".join(pts)

    lines = ""
    for values, color in [
        (danmaku, "#4a90d9"), (gift, "#e67e22"),
        (ship, "#2e8b57"), (sc, "#c0392b"),
    ]:
        lines += (f'<polyline fill="none" stroke="{color}" stroke-width="1.8" '
                  f'points="{points(values)}"/>')

    # 网格与刻度
    grid = ""
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = pad_t + h - frac * h
        grid += (f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width-pad_r}" y2="{y:.1f}" '
                 f'stroke="#e8e8e8" stroke-width="1"/>'
                 f'<text x="{pad_l-4}" y="{y+3:.1f}" font-size="9" fill="#999" '
                 f'text-anchor="end">{int(frac*peak)}</text>')

    # 日期刻度（首/中/末）
    def day_label(i):
        return history[i]["day"][5:] if i < len(history) else ""
    labels = ""
    for i in {0, n // 2, n - 1}:
        x = pad_l + i * (w / max(n - 1, 1))
        labels += (f'<text x="{x:.1f}" y="{height-6}" font-size="9" fill="#999" '
                   f'text-anchor="middle">{day_label(i)}</text>')

    return (f'<svg width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">'
            f'{grid}{lines}{labels}</svg>')
