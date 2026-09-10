# -*- coding: utf-8 -*-
"""
Web状态面板（只读，aiohttp + Jinja2 + HTMX实现）

暗色现代界面：状态卡片、机器人详情、收入趋势（区间选择）、监控房间/
动态订阅/账号池/下播记录表格。HTMX局部刷新（每30秒自动刷新摘要，趋势
区间切换无整页刷新）。前端样式经CDN加载Tailwind + HTMX。
默认监听127.0.0.1（仅本机访问），无鉴权——请勿直接暴露到公网。
通过Setting.web_panel开关，web_host/web_port配置监听。
"""

import asyncio
import json

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

    # 页面注册表：key -> (路径, 标题)
    PAGES = {
        "overview": ("/", "概览"),
        "live": ("/live", "直播监控"),
        "dynamics": ("/dynamics", "动态监控"),
        "accounts": ("/accounts", "账号池"),
        "records": ("/records", "下播记录"),
        "trend": ("/trend", "收入趋势"),
        "groups": ("/groups", "群列表"),
    }

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
        group_refresher=None,
        message_settings=None,
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
        self.group_refresher = group_refresher
        self.message_settings = message_settings
        self._runner: Optional[web.AppRunner] = None
        # 扫码登录状态（单会话）：{"status", "message", "qr", "result"}
        self._login_state: Optional[dict] = None
        self._login_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """启动HTTP服务"""
        app = web.Application()
        # 页面（HTMX请求返回内容片段，普通请求返回完整布局）
        for key, (path, _title) in self.PAGES.items():
            app.router.add_get(path, self._make_page_handler(key))
        # 局部/数据
        app.router.add_get("/panel/trend", self._handle_trend)
        app.router.add_get("/api/status", self._handle_api)
        app.router.add_get("/api/history", self._handle_history)
        # 写操作
        app.router.add_post("/web/room/add", self._action_room_add)
        app.router.add_post("/web/room/push", self._action_room_push)
        app.router.add_post("/web/room/delete", self._action_room_delete)
        app.router.add_post("/web/dynamic/bind", self._action_dynamic_bind)
        app.router.add_post("/web/dynamic/unbind", self._action_dynamic_unbind)
        app.router.add_post("/web/dynamic/delete", self._action_dynamic_delete)
        app.router.add_post("/web/account/refresh", self._action_account_refresh)
        app.router.add_post("/web/account/login/start", self._action_login_start)
        app.router.add_post("/web/account/login/cancel", self._action_login_cancel)
        app.router.add_get("/web/account/login/qr", self._handle_login_qr)
        app.router.add_get("/web/account/login/status", self._handle_login_status)
        app.router.add_post("/web/groups/refresh", self._action_groups_refresh)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info(f"Web面板已启动: http://{self.host}:{self.port}/")

    async def stop(self) -> None:
        """停止HTTP服务"""
        if self._login_task is not None and not self._login_task.done():
            self._login_task.cancel()
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

        # 数据库体积（主库+全部房间子库+动态子库；永久保留策略下的增长监控）
        db_size_mb = 0.0
        try:
            db_size_mb = await self.db_service.get_storage_size_mb()
        except Exception as e:
            logger.warning(f"统计存储体积失败: {e}")

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

    def _make_page_handler(self, key: str):
        """生成页面GET处理器（HTMX请求返回内容片段，普通请求返回完整布局）"""
        nav = [(path, key_, title) for key_, (path, title) in self.PAGES.items()]

        async def _handler(request):
            if not self._check_token(request):
                return web.Response(status=401, text="unauthorized: token required")
            ctx = await self._page_context(key)
            ctx.update(page=key, nav=nav)
            if request.headers.get("HX-Request"):
                html = self._render(f"pages/{key}.html", **ctx)
            else:
                html = self._render("base.html", page_template=f"pages/{key}.html", **ctx)
            return self._html_response(request, html)
        _handler.__name__ = f"page_{key}"
        return _handler

    async def _page_context(self, key: str) -> dict:
        """各页面公共上下文（概览=全量；子页面按需子集）"""
        data = await self._collect()
        ctx = {
            "bot": data["bot"], "me": data["me"], "app_id": data["app_id"],
            "db_size_mb": data["db_size_mb"], "accounts": data["accounts"],
            "rooms": data["rooms"], "dynamics": data["dynamics"],
            "recent": data["recent"],
            "pool_active": sum(1 for a in data["accounts"] if a["status"] == "active"),
            "pool_total": len(data["accounts"]),
        }
        if key in ("live", "dynamics", "groups"):
            ctx["group_options"] = [(g.group_openid, g.group_name)
                                    for g in await self.db_service.load_groups()]
        if key == "groups":
            ctx["groups"] = await self.db_service.load_groups()
        if key == "live":
            # 上半段：直播间注册表（推送群/监控状态），下半段：群绑定关系（含群详情）
            groups_by_openid = {}
            for g in await self.db_service.load_groups():
                groups_by_openid[g.group_openid] = g
            live_rooms = []
            for rec in await self.db_service.list_rooms():
                push_groups = self.binding_store.get_monitored_groups(rec.room_id) \
                    if self.binding_store else []
                push_names = [groups_by_openid[g].group_name if g in groups_by_openid else g
                              for g in push_groups]
                live_rooms.append({
                    "room_id": rec.room_id,
                    "streamer": await self.db_service.get_streamer_name(rec.uid)
                                if rec.uid else "",
                    "push_groups": push_names,
                    "monitored": bool(push_groups),
                    "alive": bool(push_groups) and self.monitor_manager is not None
                             and self.monitor_manager.has(rec.room_id),
                })
            ctx["rooms"] = live_rooms

            bindings = []
            for rec in await self.db_service.list_bindings():
                if not rec.monitored:
                    continue  # 已停止的推送关系不入库也不展示
                bindings.append({
                    "group_openid": rec.group_openid,
                    **self._group_info(groups_by_openid.get(rec.group_openid)),
                    "room_id": rec.room_id,
                })
            ctx["bindings"] = bindings
            ctx["push_settings"] = self._push_settings()
        if key == "dynamics":
            # 下半段：动态订阅的群绑定关系（群×UP，含群详情）
            groups_by_openid = {}
            for g in await self.db_service.load_groups():
                groups_by_openid[g.group_openid] = g
            dyn_bindings = []
            if self.binding_store is not None:
                for uid in self.binding_store.get_dynamic_up_uids():
                    up_name = self.binding_store.get_dynamic_up_name(uid)
                    for openid in self.binding_store.get_dynamic_groups(uid):
                        dyn_bindings.append({
                            "group_openid": openid,
                            **self._group_info(groups_by_openid.get(openid)),
                            "up_uid": uid,
                            "up_name": up_name,
                        })
            ctx["dyn_bindings"] = dyn_bindings
            ctx["push_settings"] = self._push_settings()
        if key == "trend":
            ctx.update(await self._trend_context("d30", ""))
        return ctx

    def _push_settings(self) -> list:
        """全局推送开关徽标（直播/动态两页的群详情共用）"""
        ms = self.message_settings
        def _on(attr):
            return bool(getattr(ms, attr, False)) if ms is not None else False
        return [
            {"label": "天选时刻", "on": _on("anchor")},
            {"label": "大航海成就", "on": _on("guard_achievement")},
            {"label": "系统事件", "on": _on("system_event")},
            {"label": "房间变更", "on": _on("room_change")},
            {"label": "动态监控", "on": _on("dynamic_monitor")},
            {"label": "每日结算", "on": _on("daily_report")},
            {"label": "每周结算", "on": _on("weekly_report")},
            {"label": "每月结算", "on": _on("monthly_report")},
        ]

    @staticmethod
    def _group_info(g) -> dict:
        """bot_group行→群详情字段（tags JSON文本转顿号分隔；缺组/空值保持None）"""
        tags = None
        if g is not None and g.tags:
            try:
                tag_list = json.loads(g.tags)
                tags = "、".join(str(t) for t in tag_list) or None
            except ValueError:
                tags = g.tags
        return {
            "group_name": g.group_name if g else None,
            "group_desc": g.group_desc if g else None,
            "category": g.category if g else None,
            "tags": tags,
            "member_count": g.member_count if g else None,
        }

    # ========== 写操作（绑定/监控/动态/账号） ==========

    def _action_response(self) -> web.Response:
        """HTMX动作响应：触发页面刷新以展示最新状态"""
        return web.Response(headers={"HX-Refresh": "true"})

    async def _read_action(self, request):
        """读取动作参数并鉴权；未授权返回None"""
        if not self._check_token(request):
            return None
        form = await request.post()
        return form

    def _form_room(self, form) -> Optional[int]:
        try:
            return int(str(form.get("room_id", "")))
        except (TypeError, ValueError):
            return None

    def _form_uid(self, form) -> Optional[int]:
        try:
            return int(str(form.get("up_uid", "")))
        except (TypeError, ValueError):
            return None

    async def _action_room_add(self, request):
        """添加直播间：归一化房间号并登记注册表（不启动监控）"""
        form = await self._read_action(request)
        if form is None:
            return web.Response(status=401, text="unauthorized: token required")
        room_id = self._form_room(form)
        if room_id is None:
            return self._action_response()
        try:
            room_info = await self.monitor_manager.get_room_info(room_id)
            real_id = int(room_info.room_id) or room_id
            if self.db_service is not None:
                await self.db_service.save_live_room_info(
                    real_id, uid=room_info.uid, title=room_info.title,
                    cover=room_info.cover)
                if room_info.uid:
                    await self.db_service.save_streamer_info(
                        room_info.uid, uname=room_info.name, room_id=real_id)
            logger.info(f"面板添加直播间：{real_id}（主播：{room_info.name}）")
        except Exception as e:
            logger.error(f"面板添加直播间失败({room_id}): {e}")
        return self._action_response()

    async def _action_room_push(self, request):
        """同步直播间推送群：保存后有推送群则启用监控，无则停止"""
        form = await self._read_action(request)
        if form is None:
            return web.Response(status=401, text="unauthorized: token required")
        room_id = self._form_room(form)
        if room_id is None:
            return self._action_response()
        target = {str(g) for g in form.getall("groups", []) if str(g)}
        try:
            current = set(self.binding_store.get_monitored_groups(room_id))                 if self.binding_store else set()
            uid = await self.db_service.get_room_uid(room_id)                 if self.db_service else 0
            for g in target - current:
                await self.binding_store.bind(g, room_id, uid)
                await self.binding_store.set_monitored(g, room_id, True)
            for g in current - target:
                await self.binding_store.unbind(g, room_id)
            if target:
                await self.monitor_manager.ensure(room_id)
            elif self.monitor_manager is not None:
                await self.monitor_manager.remove(room_id)
            logger.info(f"面板同步推送群：房间{room_id} → {sorted(target)}")
        except Exception as e:
            logger.error(f"面板同步推送群失败(房间{room_id}): {e}")
        return self._action_response()

    async def _action_room_delete(self, request):
        """删除直播间：解除全部群绑定、停止监控、移除注册（子库数据保留）"""
        form = await self._read_action(request)
        if form is None:
            return web.Response(status=401, text="unauthorized: token required")
        room_ids = [int(v) for v in form.getall("room_id", []) if str(v).isdigit()]
        for rid in room_ids:
            try:
                if self.binding_store is not None:
                    for g in self.binding_store.get_monitored_groups(rid):
                        await self.binding_store.unbind(g, rid)
                if self.monitor_manager is not None:
                    await self.monitor_manager.remove(rid)
                if self.db_service is not None:
                    await self.db_service.delete_live_room(rid)
                logger.info(f"面板删除直播间：{rid}")
            except Exception as e:
                logger.error(f"面板删除直播间失败({rid}): {e}")
        return self._action_response()

    async def _action_dynamic_bind(self, request):
        form = await self._read_action(request)
        if form is None:
            return web.Response(status=401, text="unauthorized: token required")
        group = str(form.get("group_openid", ""))
        up_uid = self._form_uid(form)
        if not group or up_uid is None:
            return self._action_response()
        try:
            await self.binding_store.bind_dynamic(group, up_uid)
            if self.dynamic_manager is not None:
                await self.dynamic_manager.ensure(up_uid)
            logger.info(f"面板绑定动态：群{group} UP主{up_uid}")
        except Exception as e:
            logger.error(f"面板绑定动态失败(群{group} UP{up_uid}): {e}")
        return self._action_response()

    async def _action_dynamic_unbind(self, request):
        form = await self._read_action(request)
        if form is None:
            return web.Response(status=401, text="unauthorized: token required")
        group = str(form.get("group_openid", ""))
        up_uid = self._form_uid(form)
        if not group or up_uid is None:
            return self._action_response()
        try:
            removed = await self.binding_store.unbind_dynamic(group, up_uid)
            if removed and self.dynamic_manager is not None                     and not self.binding_store.get_dynamic_groups(up_uid):
                await self.dynamic_manager.remove(up_uid)
            logger.info(f"面板取消动态：群{group} UP主{up_uid}")
        except Exception as e:
            logger.error(f"面板取消动态失败(群{group} UP{up_uid}): {e}")
        return self._action_response()

    async def _action_account_refresh(self, request):
        form = await self._read_action(request)
        if form is None:
            return web.Response(status=401, text="unauthorized: token required")
        uid = self._form_uid(form)
        if uid is None:
            return self._action_response()
        account = self.cookie_pool.get_account(uid) if self.cookie_pool else None
        if account is None:
            return self._action_response()
        from .bili_login import BiliLoginService
        service = BiliLoginService()
        logger.info(f"面板触发刷新Cookie：uid={uid}")
        try:
            result = await service.refresh(account.cookie_string)
        except Exception as e:
            logger.error(f"面板刷新Cookie异常(uid={uid}): {e}")
            return self._action_response()
        if result.ok and result.uid == account.uid:
            await self.cookie_pool.upsert(
                uid=result.uid, nickname=result.nickname,
                cookie_string=result.cookie_string, sessdata=result.sessdata,
                bili_jct=result.bili_jct, ac_time_value=result.ac_time_value,
                sessdata_expires_at=result.sessdata_expires_at)
            logger.info(f"账号{uid} Cookie刷新成功")
        else:
            logger.warning(f"账号{uid} Cookie刷新失败（登录态失效？）")
        return self._action_response()

    # ========== 扫码登录（Web端） ==========

    async def _action_login_start(self, request):
        if await self._read_action(request) is None:
            return web.Response(status=401, text="unauthorized: token required")
        if self._login_task is not None and not self._login_task.done():
            return self._action_response()
        from .bili_login import BiliLoginService

        state = {"status": "starting", "message": "正在启动浏览器…", "qr": None}
        self._login_state = state

        async def on_qrcode(png_bytes, qr_url):
            state["qr"] = png_bytes
            state["status"] = "waiting"
            state["message"] = "请使用B站APP扫码"

        async def _run():
            service = BiliLoginService(on_qrcode=on_qrcode, timeout=60)
            try:
                result = await service.login()
            except asyncio.CancelledError:
                state.update(status="cancelled", message="登录已取消")
                raise
            except Exception as e:
                state.update(status="failed", message=f"登录异常: {e}")
                return
            if result is not None and getattr(result, "ok", False):
                state.update(status="success",
                             message=f"登录成功：{result.nickname}（UID:{result.uid}）")
                if self.cookie_pool is not None:
                    await self.cookie_pool.upsert(
                        uid=result.uid, nickname=result.nickname,
                        cookie_string=result.cookie_string,
                        sessdata=result.sessdata, bili_jct=result.bili_jct,
                        ac_time_value=result.ac_time_value,
                        sessdata_expires_at=result.sessdata_expires_at)
                logger.info(f"面板扫码登录成功：uid={result.uid}")
            else:
                state.update(status="failed", message="登录失败或超时（可重新开始）")

        self._login_task = asyncio.create_task(_run(), name="web-login")
        return self._action_response()

    async def _action_login_cancel(self, request):
        if await self._read_action(request) is None:
            return web.Response(status=401, text="unauthorized: token required")
        if self._login_task is not None and not self._login_task.done():
            self._login_task.cancel()
        return self._action_response()

    async def _handle_login_qr(self, request):
        if not self._check_token(request):
            return web.Response(status=401, text="unauthorized")
        qr = (self._login_state or {}).get("qr")
        if not qr:
            return web.Response(status=404, text="no qrcode")
        return web.Response(body=qr, content_type="image/png")

    async def _handle_login_status(self, request):
        if not self._check_token(request):
            return web.Response(status=401, text="unauthorized")
        state = self._login_state or {}
        return web.json_response({"status": state.get("status", ""),
                                  "message": state.get("message", "")})

    async def _action_dynamic_delete(self, request):
        """批量取消动态订阅：按up_uid解除全部群绑定并停止轮询进程"""
        form = await self._read_action(request)
        if form is None:
            return web.Response(status=401, text="unauthorized: token required")
        up_uids = [int(v) for v in form.getall("up_uid", []) if str(v).isdigit()]
        for up_uid in up_uids:
            try:
                if self.binding_store is not None:
                    for group_openid in self.binding_store.get_dynamic_groups(up_uid):
                        await self.binding_store.unbind_dynamic(group_openid, up_uid)
                if self.dynamic_manager is not None:
                    await self.dynamic_manager.remove(up_uid)
                logger.info(f"面板取消动态订阅：UP主{up_uid}")
            except Exception as e:
                logger.error(f"面板取消动态订阅失败(UP{up_uid}): {e}")
        return self._action_response()

    async def _action_groups_refresh(self, request):
        if not self._check_token(request):
            return web.Response(status=401, text="unauthorized: token required")
        if self.group_refresher is not None:
            count = await self.group_refresher.refresh_once()
            logger.info(f"面板触发群信息刷新：{count}个群")
        return self._action_response()

    async def _handle_trend(self, request):
        if not self._check_token(request):
            return web.Response(status=401, text="unauthorized: token required")
        room_raw = (request.query.get("room") or "").strip()
        room_id = int(room_raw) if room_raw.isdigit() else None
        ctx = await self._trend_context(
            request.query.get("range", "d30"), request.query.get("month", ""),
            room_id=room_id)
        html = self._render("partials/trend.html", **ctx)
        return self._html_response(request, html)

    async def _trend_context(self, range_key: str, month_str: str,
                             room_id: Optional[int] = None) -> dict:
        """
        趋势数据上下文：近N天 / 本月 / 上月 / 指定自然月 / 近12个月（按月汇总）

        Args:
            room_id: 指定直播间（None=全部直播间总和）
        """
        today = datetime.now().date()
        rows: list = []
        mode = "daily"
        active = "d30"
        label = "近30天"
        month_value = today.strftime("%Y-%m")
        room_ids = [room_id] if room_id else None

        if range_key in ("d7", "d14", "d30"):
            n = int(range_key[1:])
            active = range_key
            label = f"近{n}天"
            rows = await self.db_service.get_daily_history_range(
                _add_days(today, -(n - 1)), today, room_ids=room_ids)
        elif range_key == "this_month":
            active = range_key
            label = f"{today.year}年{today.month}月"
            rows = await self.db_service.get_daily_history_range(
                today.replace(day=1), today, room_ids=room_ids)
        elif range_key == "last_month":
            first_this = today.replace(day=1)
            last_prev = _add_days(first_this, -1)
            start = last_prev.replace(day=1)
            active = range_key
            label = f"{last_prev.year}年{last_prev.month}月"
            rows = await self.db_service.get_daily_history_range(
                start, last_prev, room_ids=room_ids)
        elif range_key == "12m":
            active = range_key
            mode = "monthly"
            label = "近12个月（按月汇总）"
            first_this = today.replace(day=1)
            start = _add_days(first_this, -1).replace(day=1)
            for _ in range(10):
                start = _add_days(start, -1).replace(day=1)
            daily = await self.db_service.get_daily_history_range(
                start, today, room_ids=room_ids)
            rows = _aggregate_monthly(daily)
        elif (range_key == "custom_month" or range_key.startswith("month:"))                 and _parse_month(month_str):
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
            rows = await self.db_service.get_daily_history_range(
                start, end, room_ids=room_ids)
        else:
            rows = await self.db_service.get_daily_history_range(
                _add_days(today, -29), today, room_ids=room_ids)

        if room_id:
            label += f" · 房间{room_id}"

        room_options = set()
        if self.binding_store is not None:
            try:
                room_options.update(self.binding_store.get_monitored_room_ids())
            except Exception:
                pass
        try:
            room_options.update(await self.db_service.list_room_ids())
        except Exception:
            pass

        return {
            "data": rows, "mode": mode, "active": active, "label": label,
            "month_value": month_value,
            "this_month_key": today.strftime("%Y-%m"),
            "room_id": room_id or "",
            "room_options": sorted(room_options),
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
