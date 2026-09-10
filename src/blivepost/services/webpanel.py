# -*- coding: utf-8 -*-
"""
Web状态面板（aiohttp + Jinja2 + HTMX实现）

暗色现代界面：状态卡片、机器人详情、收入趋势（区间选择）、监控房间/
动态订阅/账号池/下播记录表格。HTMX局部刷新（趋势区间切换无整页刷新）。
前端样式经CDN加载Tailwind + HTMX。
默认监听127.0.0.1（仅本机访问）。
鉴权模型：打开默认游客（只读，动作接口全部拒绝）；前台用户登录后可控
（账号由后台管理，存web_user表）；管理员来自Setting.web_admin_user/
web_admin_password（口令非空才启用后台），可管用户/全局推送开关/导出数据；
Setting.web_token保留为管理员快捷令牌（?token=即管理员登录）。
通过Setting.web_panel开关，web_host/web_port配置监听。
"""

import asyncio
import hashlib
import io
import json
import secrets
import zipfile

from datetime import date, datetime, timedelta
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

_SESSION_COOKIE = "blivepost_sid"
_SESSION_TTL = timedelta(days=7)
_PBKDF2_ITERATIONS = 120_000

# 全局推送开关注册表：name -> (显示名, config默认值字段)
SWITCH_DEFS = [
    ("anchor", "天选时刻"),
    ("guard_achievement", "大航海成就"),
    ("system_event", "系统事件"),
    ("room_change", "房间变更"),
    ("dynamic_monitor", "动态监控"),
    ("daily_report", "每日结算"),
    ("weekly_report", "每周结算"),
    ("monthly_report", "每月结算"),
]


def hash_password(password: str) -> str:
    """pbkdf2口令哈希（随机盐，格式 pbkdf2$iterations$salt$hash）"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令哈希（格式不合法/不匹配返回False）"""
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                   bytes.fromhex(salt), int(iterations))
        return secrets.compare_digest(calc.hex(), digest)
    except (ValueError, TypeError):
        return False


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
        "wordcloud": ("/wordcloud", "词云"),
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
        admin_user: str = "admin",
        admin_password: str = "",
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
        # 后台管理员（config设置；口令非空才启用管理员登录）
        self.admin_user = admin_user or "admin"
        self.admin_password = admin_password
        # 内存会话：sid -> {"user", "admin", "exp"}
        self._sessions: dict = {}
        self._runner: Optional[web.AppRunner] = None
        # 扫码登录状态（单会话）：{"status", "message", "qr", "result"}
        self._login_state: Optional[dict] = None
        self._login_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """启动HTTP服务"""
        app = web.Application()
        # 页面（HTMX请求返回内容片段，普通请求返回完整布局；游客只读可浏览）
        for key, (path, _title) in self.PAGES.items():
            app.router.add_get(path, self._make_page_handler(key))
        # 登录/登出
        app.router.add_get("/login", self._handle_login_page)
        app.router.add_post("/web/login", self._action_web_login)
        app.router.add_post("/web/logout", self._action_web_logout)
        # 后台（管理员）
        app.router.add_get("/admin", self._handle_admin_page)
        app.router.add_post("/web/admin/user/add", self._action_admin_user_add)
        app.router.add_post("/web/admin/user/password", self._action_admin_user_password)
        app.router.add_post("/web/admin/user/delete", self._action_admin_user_delete)
        app.router.add_post("/web/admin/switch", self._action_admin_switch)
        app.router.add_get("/web/admin/export", self._handle_admin_export)
        # 全局开关（前台可改项：登录用户；管理员不受限）
        app.router.add_post("/web/switch/toggle", self._action_switch_toggle)
        # 群独立推送设置（直播页群详情）
        app.router.add_post("/web/room/push_setting", self._action_group_push_setting)
        # 局部/数据
        app.router.add_get("/panel/trend", self._handle_trend)
        app.router.add_get("/panel/wordcloud", self._handle_wordcloud)
        app.router.add_get("/api/status", self._handle_api)
        app.router.add_get("/api/history", self._handle_history)
        # 写操作（游客拒绝；登录用户可用）
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

    # ========== 会话与鉴权（游客只读/用户可控/管理员可管） ==========

    def _create_session(self, user: str, is_admin: bool) -> str:
        """新建会话（7天有效），返回会话ID"""
        self._prune_sessions()
        sid = secrets.token_urlsafe(32)
        self._sessions[sid] = {
            "user": user, "admin": bool(is_admin),
            "exp": (datetime.now() + _SESSION_TTL).timestamp(),
        }
        return sid

    def _prune_sessions(self) -> None:
        now = datetime.now().timestamp()
        for sid in [s for s, v in self._sessions.items() if v["exp"] < now]:
            self._sessions.pop(sid, None)

    def _session(self, request) -> Optional[dict]:
        """当前会话（None=游客）"""
        sid = request.cookies.get(_SESSION_COOKIE, "")
        sess = self._sessions.get(sid)
        if sess is None or sess["exp"] < datetime.now().timestamp():
            return None
        return sess

    def _current_user(self, request) -> Optional[str]:
        sess = self._session(request)
        return sess["user"] if sess else None

    def _is_admin(self, request) -> bool:
        sess = self._session(request)
        return bool(sess and sess["admin"])

    def _web_token_ok(self, request) -> bool:
        """管理员快捷令牌校验（未配置token时不可用）"""
        if not self.token:
            return False
        provided = (request.query.get("token")
                    or request.headers.get("X-Web-Token")
                    or request.cookies.get("web_token")
                    or "")
        return provided == self.token

    def _set_session_cookie(self, resp: web.Response, sid: str) -> None:
        resp.set_cookie(_SESSION_COOKIE, sid, max_age=int(_SESSION_TTL.total_seconds()),
                        httponly=True, samesite="Lax", path="/")

    async def _auth_token_to_admin(self, request):
        """?token=管理员快捷令牌→创建管理员会话并重定向到干净URL（无令牌返回None）"""
        if not (self.token and request.query.get("token") == self.token):
            return None
        sid = self._create_session(self.admin_user, is_admin=True)
        target = request.path or "/"
        resp = web.HTTPFound(target)
        self._set_session_cookie(resp, sid)
        resp.set_cookie("web_token", self.token, httponly=True, samesite="Lax")
        logger.info(f"Web面板令牌登录（管理员）：{request.remote}")
        return resp

    async def _require_login(self, request) -> Optional[web.Response]:
        """动作接口鉴权：游客拒绝（登录用户与管理员放行）"""
        if self._session(request) is None and not self._web_token_ok(request):
            return web.Response(status=401, text="unauthorized: 请先登录（游客只读）")
        return None

    async def _require_admin(self, request) -> Optional[web.Response]:
        """后台接口鉴权：仅管理员会话（web_token快捷令牌视同管理员）"""
        if not self._is_admin(request) and not self._web_token_ok(request):
            return web.Response(status=403, text="forbidden: 需要管理员权限")
        return None

    def _html_response(self, request, html: str) -> web.Response:
        """HTML响应"""
        return web.Response(text=html, content_type="text/html", charset="utf-8")

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
            if self.db_service is not None:
                db_size_mb = await self.db_service.get_storage_size_mb()
        except Exception as e:
            logger.warning(f"统计存储体积失败: {e}")

        accounts = []
        for a in (self.cookie_pool.accounts if self.cookie_pool else []):
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
            s = await self.db_service.get_daily_summary(rid, day=now)                 if self.db_service else {"danmaku_count": 0, "gift_gold": 0,
                                         "ship_total": 0, "sc_total": 0}
            rooms.append({
                "room_id": rid,
                "alive": self.monitor_manager.has(rid),
                "uid": self.monitor_manager.get_cookie_uid(rid),
                "streamer": await self._streamer_name(rid),
                "danmaku": s["danmaku_count"],
                "gold": s["gift_gold"], "ship": s["ship_total"], "sc": s["sc_total"],
            })

        dynamics = []
        for uid in self.binding_store.get_dynamic_up_uids()                 if self.binding_store else []:
            dynamics.append({
                "up_uid": uid,
                "name": self.binding_store.get_dynamic_up_name(uid),
                "alive": self.dynamic_manager.has(uid) if self.dynamic_manager else False,
                "groups": len(self.binding_store.get_dynamic_groups(uid)),
            })

        recent = await self.db_service.get_recent_live_status(limit=10)             if self.db_service else []
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
        """生成页面GET处理器（HTMX请求返回内容片段，普通请求返回完整布局；游客可浏览）"""
        nav = [(path, key_, title) for key_, (path, title) in self.PAGES.items()]

        async def _handler(request):
            # ?token=管理员快捷令牌：建管理员会话并重定向到干净URL
            token_resp = await self._auth_token_to_admin(request)
            if token_resp is not None:
                return token_resp
            ctx = await self._page_context(key)
            ctx.update(page=key, nav=nav, current_user=self._current_user(request),
                       is_admin=self._is_admin(request))
            if request.headers.get("HX-Request"):
                html = self._render(f"pages/{key}.html", **ctx)
            else:
                html = self._render("base.html", page_template=f"pages/{key}.html", **ctx)
            return self._html_response(request, html)
        _handler.__name__ = f"page_{key}"
        return _handler

    # ========== 登录/登出/后台页面 ==========

    async def _handle_login_page(self, request):
        """登录页（游客可访问；已登录重定向首页）"""
        token_resp = await self._auth_token_to_admin(request)
        if token_resp is not None:
            return token_resp
        if self._session(request) is not None:
            raise web.HTTPFound("/")
        ctx = {
            "page": "login", "nav": [], "current_user": None, "is_admin": False,
            "error": request.query.get("err", ""),
            "admin_enabled": bool(self.admin_password),
        }
        html = self._render("base.html", page_template="pages/login.html", **ctx)
        return self._html_response(request, html)

    async def _action_web_login(self, request):
        """登录：先匹配config管理员，再匹配web_user表；成功建会话重定向首页"""
        form = await request.post()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        user, is_admin = None, False
        if self.admin_password and username == self.admin_user \
                and secrets.compare_digest(password, self.admin_password):
            user, is_admin = self.admin_user, True
        else:
            record = await self.db_service.get_web_user(username) \
                if self.db_service is not None else None
            if record is not None and verify_password(password, record["password_hash"]):
                user = username
        if user is None:
            logger.warning(f"Web面板登录失败：{username}（{request.remote}）")
            raise web.HTTPFound("/login?err=1")
        sid = self._create_session(user, is_admin)
        resp = web.HTTPFound("/admin" if is_admin else "/")
        self._set_session_cookie(resp, sid)
        logger.info(f"Web面板登录：{user}（{'管理员' if is_admin else '用户'}，{request.remote}）")
        return resp

    async def _action_web_logout(self, request):
        """登出：销毁会话回到游客"""
        sid = request.cookies.get(_SESSION_COOKIE, "")
        self._sessions.pop(sid, None)
        raise web.HTTPFound("/")

    def _require_admin_page(self, request) -> Optional[web.Response]:
        """后台页面鉴权：管理员会话/web_token放行，否则跳登录页"""
        if self._is_admin(request) or self._web_token_ok(request):
            return None
        return web.HTTPFound("/login?err=2")

    async def _handle_admin_page(self, request):
        """管理后台：用户管理/推送开关/数据导出"""
        denied = self._require_admin_page(request)
        if denied is not None:
            raise denied
        nav = [(path, key_, title) for key_, (path, title) in self.PAGES.items()]
        users = await self.db_service.list_web_users() \
            if self.db_service is not None else []
        switches = await self.db_service.get_push_switches() \
            if self.db_service is not None else {}
        admin_ctx = {
            "users": users,
            "switches": [
                {"name": name, "label": label,
                 "enabled": switches.get(name, {}).get("enabled", False),
                 "allow_modify": switches.get(name, {}).get("allow_modify", False)}
                for name, label in SWITCH_DEFS
            ],
            "sqlite_export": True,
        }
        ctx = {"page": "admin", "nav": nav, "current_user": self._current_user(request),
               "is_admin": True, **admin_ctx}
        html = self._render("base.html", page_template="pages/admin.html", **ctx)
        return self._html_response(request, html)

    # ========== 后台动作（仅管理员） ==========

    async def _action_admin_user_add(self, request):
        denied = await self._require_admin(request)
        if denied is not None:
            return denied
        form = await request.post()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        if not username or len(password) < 4:
            return self._action_response()
        created = await self.db_service.create_web_user(username, hash_password(password))
        logger.info(f"后台{'添加' if created else '添加失败(已存在?)'}用户：{username}")
        return self._action_response()

    async def _action_admin_user_password(self, request):
        denied = await self._require_admin(request)
        if denied is not None:
            return denied
        form = await request.post()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        if not username or len(password) < 4:
            return self._action_response()
        updated = await self.db_service.update_web_user_password(
            username, hash_password(password))
        logger.info(f"后台{'重置' if updated else '重置失败(不存在?)'}用户口令：{username}")
        return self._action_response()

    async def _action_admin_user_delete(self, request):
        denied = await self._require_admin(request)
        if denied is not None:
            return denied
        form = await request.post()
        username = str(form.get("username", "")).strip()
        if not username:
            return self._action_response()
        deleted = await self.db_service.delete_web_user(username)
        logger.info(f"后台{'删除' if deleted else '删除失败(不存在?)'}用户：{username}")
        return self._action_response()

    async def _action_admin_switch(self, request):
        """更新推送开关（状态/允许前台修改）；动态监控联动轮询进程启停"""
        denied = await self._require_admin(request)
        if denied is not None:
            return denied
        form = await request.post()
        name = str(form.get("name", ""))
        if name not in {n for n, _ in SWITCH_DEFS}:
            return self._action_response()
        enabled = form.get("enabled", "")
        allow = form.get("allow_modify", "")
        try:
            await self.db_service.set_push_switch(
                name,
                enabled=bool(int(enabled)) if enabled != "" else None,
                allow_modify=bool(int(allow)) if allow != "" else None)
        except Exception as e:
            logger.error(f"后台更新推送开关失败({name}): {e}")
            return self._action_response()
        # 动态监控开关联动：关→停全部轮询；开→恢复全部订阅UP轮询
        if name == "dynamic_monitor" and enabled != "":
            await self._apply_dynamic_switch(bool(int(enabled)))
        logger.info(f"后台更新推送开关：{name} enabled={enabled} allow_modify={allow}")
        return self._action_response()

    async def _apply_dynamic_switch(self, on: bool) -> None:
        """动态监控开关生效：关闭停全部轮询进程，开启恢复全部订阅UP"""
        if self.dynamic_manager is None or self.binding_store is None:
            return
        if not on:
            await self.dynamic_manager.stop_all()
            return
        for up_uid in self.binding_store.get_dynamic_up_uids():
            try:
                await self.dynamic_manager.ensure(up_uid)
            except Exception as e:
                logger.error(f"恢复UP主{up_uid}动态轮询失败: {e}")

    async def _handle_admin_export(self, request):
        """导出数据：主库+全部房间子库+动态子库打包zip下载（仅SQLite）"""
        denied = await self._require_admin(request)
        if denied is not None:
            return denied
        db_path = Path(self._sqlite_path())
        if db_path is None:
            return web.Response(status=409, text="仅SQLite存储支持导出")
        if not db_path.exists():
            return web.Response(status=404, text="数据库文件不存在")
        data_dir = db_path.parent
        buf = await asyncio.get_running_loop().run_in_executor(
            None, self._build_export_zip, db_path, data_dir)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return web.Response(
            body=buf.getvalue(), content_type="application/zip",
            headers={"Content-Disposition":
                     f'attachment; filename="blivepost_export_{stamp}.zip"'})

    def _sqlite_path(self) -> Optional[str]:
        """当前主库文件路径（非SQLite配置返回None）"""
        getter = getattr(self.db_service, "sqlite_path", None) \
            if self.db_service is not None else None
        return getter() if callable(getter) else None

    @staticmethod
    def _build_export_zip(db_path: Path, data_dir: Path) -> "io.BytesIO":
        """打包主库+rooms/+dynamics/全部sqlite文件"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_path, arcname=f"data/{db_path.name}")
            for sub in ("rooms", "dynamics"):
                root = data_dir / sub
                if not root.exists():
                    continue
                for f in sorted(root.rglob("*.db")):
                    zf.write(f, arcname=str(f.relative_to(data_dir).as_posix()))
        return buf

    # ========== 群独立推送设置（直播页群详情） ==========

    async def _action_group_push_setting(self, request):
        """切换本群独立推送开关；reset=1时删除行恢复跟随全局"""
        denied = await self._require_login(request)
        if denied is not None:
            return denied
        form = await request.post()
        room_id = self._form_room(form)
        group = str(form.get("group_openid", ""))
        name = str(form.get("name", ""))
        reset = str(form.get("reset", "")) == "1"
        if room_id is None or not group:
            return self._action_response()
        if reset:
            await self.db_service.reset_group_push_setting(room_id, group)
            logger.info(f"面板重置群推送设置：房间{room_id} 群{group} 恢复跟随全局")
            return self._action_response()
        if name not in {n for n, _ in SWITCH_DEFS}:
            return self._action_response()
        # 前台用户需该开关全局allow_modify放行；管理员不受限
        if not self._is_admin(request) and not self._web_token_ok(request):
            switches = await self.db_service.get_push_switches()                 if self.db_service is not None else {}
            if not switches.get(name, {}).get("allow_modify", False):
                return web.Response(status=403, text="forbidden: 该开关不允许前台修改")
        own = await self.db_service.get_group_push_setting(room_id, group)
        current = bool(own and own.get(name))
        new_state = not current
        await self.db_service.set_group_push_setting(room_id, group, name, new_state)
        logger.info(f"面板切换群推送设置：房间{room_id} 群{group} {name} → "
                    f"{'开' if new_state else '关'}")
        return self._action_response()

    # ========== 全局开关（前台可改项） ==========

    async def _action_switch_toggle(self, request):
        """切换推送开关状态：管理员任意改；前台用户仅改allow_modify=开的项"""
        denied = await self._require_login(request)
        if denied is not None:
            return denied
        form = await request.post()
        name = str(form.get("name", ""))
        if name not in {n for n, _ in SWITCH_DEFS}:
            return self._action_response()
        switches = await self.db_service.get_push_switches() \
            if self.db_service is not None else {}
        info = switches.get(name, {})
        if not self._is_admin(request) and not self._web_token_ok(request) \
                and not info.get("allow_modify", False):
            return web.Response(status=403, text="forbidden: 该开关不允许前台修改")
        new_state = not info.get("enabled", False)
        await self.db_service.set_push_switch(name, enabled=new_state)
        if name == "dynamic_monitor":
            await self._apply_dynamic_switch(new_state)
        logger.info(f"面板切换推送开关：{name} → {'开' if new_state else '关'}"
                    f"（{'管理员' if self._is_admin(request) else '用户'}）")
        return self._action_response()

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

            global_sw = {ps["name"]: ps for ps in await self._push_settings()}
            bindings = []
            for rec in await self.db_service.list_bindings():
                if not rec.monitored:
                    continue  # 已停止的推送关系不入库也不展示
                own = await self.db_service.get_group_push_setting(
                    rec.room_id, rec.group_openid)
                push_items = []
                custom_any = False
                for name, label in SWITCH_DEFS:
                    if name == "dynamic_monitor":
                        continue  # 动态监控为全局项，不随房间
                    g = global_sw.get(name, {})
                    glob_on = g.get("on", False)
                    # custom=本群值偏离全局（行随绑定自动播种，偏离才视为独立设置）
                    custom = own is not None and own[name] != glob_on
                    custom_any = custom_any or custom
                    push_items.append({
                        "name": name, "label": label,
                        "on": own[name] if own else glob_on,
                        "custom": custom,
                        "allow_modify": g.get("allow_modify", False),
                    })
                bindings.append({
                    "group_openid": rec.group_openid,
                    **self._group_info(groups_by_openid.get(rec.group_openid)),
                    "room_id": rec.room_id,
                    "push_settings": push_items,
                    "has_custom": custom_any,
                })
            ctx["bindings"] = bindings
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
            ctx["push_settings"] = await self._push_settings()
        if key == "trend":
            ctx.update(await self._trend_context("d30", ""))
        if key == "wordcloud":
            ctx.update(await self._wordcloud_context("today"))
        return ctx

    async def _push_settings(self) -> list:
        """全局推送开关徽标（直播/动态两页的群详情共用；DB运行时值优先）"""
        ms = self.message_settings
        def _default(attr):
            return bool(getattr(ms, attr, False)) if ms is not None else False
        switches = {}
        if self.db_service is not None:
            try:
                switches = await self.db_service.get_push_switches()
            except Exception:
                switches = {}
        return [
            {"name": name, "label": label,
             "on": switches.get(name, {}).get("enabled", _default(name)),
             "allow_modify": switches.get(name, {}).get("allow_modify", False)}
            for name, label in SWITCH_DEFS
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
        """读取动作参数并鉴权（登录用户/管理员/web_token；游客拒绝）；未授权返回None"""
        if await self._require_login(request) is not None:
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
            return web.Response(status=401, text="unauthorized: 请先登录（游客只读）")
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
            return web.Response(status=401, text="unauthorized: 请先登录（游客只读）")
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
            return web.Response(status=401, text="unauthorized: 请先登录（游客只读）")
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
            return web.Response(status=401, text="unauthorized: 请先登录（游客只读）")
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
            return web.Response(status=401, text="unauthorized: 请先登录（游客只读）")
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
            return web.Response(status=401, text="unauthorized: 请先登录（游客只读）")
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
            return web.Response(status=401, text="unauthorized: 请先登录（游客只读）")
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
            return web.Response(status=401, text="unauthorized: 请先登录（游客只读）")
        if self._login_task is not None and not self._login_task.done():
            self._login_task.cancel()
        return self._action_response()

    async def _handle_login_qr(self, request):
        if await self._require_login(request) is not None:
            return web.Response(status=401, text="unauthorized")
        qr = (self._login_state or {}).get("qr")
        if not qr:
            return web.Response(status=404, text="no qrcode")
        return web.Response(body=qr, content_type="image/png")

    async def _handle_login_status(self, request):
        if await self._require_login(request) is not None:
            return web.Response(status=401, text="unauthorized")
        state = self._login_state or {}
        return web.json_response({"status": state.get("status", ""),
                                  "message": state.get("message", "")})

    async def _action_dynamic_delete(self, request):
        """批量取消动态订阅：按up_uid解除全部群绑定并停止轮询进程"""
        form = await self._read_action(request)
        if form is None:
            return web.Response(status=401, text="unauthorized: 请先登录（游客只读）")
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
        if await self._require_login(request) is not None:
            return web.Response(status=401, text="unauthorized: 请先登录（游客只读）")
        if self.group_refresher is not None:
            count = await self.group_refresher.refresh_once()
            logger.info(f"面板触发群信息刷新：{count}个群")
        return self._action_response()

    async def _handle_trend(self, request):
        room_raw = (request.query.get("room") or "").strip()
        room_id = int(room_raw) if room_raw.isdigit() else None
        ctx = await self._trend_context(
            request.query.get("range", "d30"), request.query.get("month", ""),
            room_id=room_id)
        html = self._render("partials/trend.html", **ctx)
        return self._html_response(request, html)

    async def _handle_wordcloud(self, request):
        """词云局部（HTMX交换；游客只读可看）"""
        room_raw = (request.query.get("room") or "").strip()
        room_id = int(room_raw) if room_raw.isdigit() else None
        ctx = await self._wordcloud_context(
            request.query.get("range", "today"),
            day_str=request.query.get("day", ""),
            month_str=request.query.get("month", ""),
            year_str=request.query.get("year", ""),
            room_id=room_id)
        html = self._render("partials/wordcloud.html", **ctx)
        return self._html_response(request, html)

    async def _wordcloud_context(self, range_key: str, day_str: str = "",
                                 month_str: str = "", year_str: str = "",
                                 room_id: Optional[int] = None) -> dict:
        """
        词云上下文：按天（今天/昨天/指定天）、近3/7/14天、按月（本月/上月/指定月）、
        按年（今年/去年/指定年），可切全部/单独直播间

        Returns:
            dict: {label, words, total, room_id, active, day/month/year值, room_options}
        """
        today = datetime.now().date()
        start = end = today
        active = range_key or "today"
        label = "今天"
        day_value = today.strftime("%Y-%m-%d")
        month_value = today.strftime("%Y-%m")
        year_value = str(today.year)

        def _month_bounds(y: int, m: int):
            start_m = date(y, m, 1)
            next_m = date(y + (m == 12), (m % 12) + 1, 1)
            return start_m, _add_days(next_m, -1)

        if range_key == "today":
            label = "今天"
        elif range_key == "yesterday":
            start = end = _add_days(today, -1)
            label = "昨天"
        elif range_key == "day" and _parse_day(day_str):
            start = end = _parse_day(day_str)
            day_value = start.strftime("%Y-%m-%d")
            active = "day"
            label = f"{start.year}年{start.month}月{start.day}日"
        elif range_key in ("d3", "d7", "d14"):
            n = int(range_key[1:])
            start = _add_days(today, -(n - 1))
            label = f"近{n}天"
        elif range_key == "month":
            start, end = _month_bounds(today.year, today.month)
            end = min(end, today)
            label = f"{today.year}年{today.month}月"
        elif range_key == "lastmonth":
            first_this = today.replace(day=1)
            last_prev = _add_days(first_this, -1)
            start = last_prev.replace(day=1)
            end = last_prev
            label = f"{last_prev.year}年{last_prev.month}月"
        elif range_key.startswith("month:") and _parse_month(month_str):
            y, m = _parse_month(month_str)
            start, end = _month_bounds(y, m)
            end = min(end, today)
            if start > today:
                start = end = today
            active = f"month:{month_str}"
            month_value = month_str
            label = f"{y}年{m}月"
        elif range_key == "year":
            start = date(today.year, 1, 1)
            label = f"{today.year}年"
        elif range_key == "lastyear":
            start = date(today.year - 1, 1, 1)
            end = date(today.year - 1, 12, 31)
            label = f"{today.year - 1}年"
        elif range_key.startswith("year:") and year_str.isdigit()                 and 2000 <= int(year_str) <= today.year:
            y = int(year_str)
            start = date(y, 1, 1)
            end = min(date(y, 12, 31), today)
            active = f"year:{year_str}"
            year_value = year_str
            label = f"{y}年"
        else:
            active = "today"

        room_ids = [room_id] if room_id else None
        texts = await self.db_service.get_danmaku_texts(
            room_ids=room_ids, start_day=start, end_day=end)
        from .wordcloud import build_wordcloud
        words = await asyncio.get_running_loop().run_in_executor(
            None, build_wordcloud, texts)
        label += f" · 房间{room_id}" if room_id else ""

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
            "label": label, "words": words, "total": len(texts),
            "danmaku_count": len(texts),
            "room_id": room_id, "active": active,
            "day_value": day_value, "month_value": month_value,
            "year_value": year_value,
            "this_month_key": month_value, "this_year": str(today.year),
            "room_options": sorted(room_options),
        }

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
        try:
            days = max(min(int(request.query.get("days", 30)), 365), 1)
        except ValueError:
            days = 30
        history = await self.db_service.get_daily_history(days=days)             if self.db_service else []
        return web.json_response({"days": days, "history": history})

    async def _handle_api(self, request):
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


def _parse_day(text: str):
    """YYYY-MM-DD→date（非法返回None）"""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
