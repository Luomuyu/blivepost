# -*- coding: utf-8 -*-
"""
Web状态面板（只读，aiohttp实现）

展示账号池状态、监控房间与进程状态、各房间当日汇总、最近下播记录。
默认监听127.0.0.1（仅本机访问），无鉴权——请勿直接暴露到公网。
通过Setting.web_panel开关，web_host/web_port配置监听。
"""

from datetime import datetime
from typing import TYPE_CHECKING, Optional

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
<h2>账号池（可用 {active}/{total}）</h2>
<table><tr><th>UID</th><th>昵称</th><th>状态</th><th>过期</th><th>最近使用</th><th>关联房间</th></tr>
{accounts}</table>
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
    ):
        self.cookie_pool = cookie_pool
        self.monitor_manager = monitor_manager
        self.dynamic_manager = dynamic_manager
        self.binding_store = binding_store
        self.db_service = db_service
        self.host = host
        self.port = port
        self._runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        """启动HTTP服务"""
        app = web.Application()
        app.router.add_get("/", self._handle_home)
        app.router.add_get("/api/status", self._handle_api)
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

    async def _collect(self) -> dict:
        """收集面板数据"""
        now = datetime.now()
        accounts = []
        for a in self.cookie_pool.accounts:
            rooms = self.monitor_manager.get_account_rooms(a.uid) if self.monitor_manager else []
            accounts.append({
                "uid": a.uid, "nickname": a.nickname, "status": a.status,
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
            "accounts": accounts,
            "rooms": rooms,
            "dynamics": dynamics,
            "recent": recent,
        }

    async def _handle_home(self, request):
        data = await self._collect()
        accounts = "".join(
            f"<tr><td>{a['uid']}</td><td>{a['nickname']}</td>"
            f"<td class=\"{'ok' if a['status'] == 'active' else 'bad'}\">{a['status']}</td>"
            f"<td>{a['expires'] or '-'}</td><td>{a['last_used'] or '-'}</td>"
            f"<td>{'、'.join(map(str, a['rooms'])) or '-'}</td></tr>"
            for a in data["accounts"])
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
        html = _PAGE.format(
            now=data["now"],
            active=sum(1 for a in data["accounts"] if a["status"] == "active"),
            total=len(data["accounts"]), accounts=accounts or "<tr><td colspan=6>空</td></tr>",
            room_count=len(data["rooms"]), rooms=rooms or "<tr><td colspan=7>无</td></tr>",
            dyn_count=len(data["dynamics"]),
            dynamics=dynamics or "<tr><td colspan=4>无</td></tr>",
            live_records=recent or "<tr><td colspan=7>无</td></tr>",
        )
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def _handle_api(self, request):
        data = await self._collect()
        return web.json_response(data)
