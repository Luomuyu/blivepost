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

实现按职责拆分为webpanel_*混入模块：鉴权与会话（webpanel_auth.AuthMixin）、
动作路由（webpanel_actions.ActionsMixin）、页面与上下文
（webpanel_pages.PagesMixin）、后台（webpanel_admin.AdminMixin）、
数据大屏（webpanel_screen.ScreenMixin）；共享的模块级对象与纯函数在
webpanel_common。本模块负责组合WebPanel与start/stop路由注册。
"""

import asyncio
from datetime import datetime

from typing import TYPE_CHECKING, Optional

from aiohttp import web

from .api_v1 import ApiV1Mixin, register_api_v1_routes
from .webpanel_actions import ActionsMixin
from .webpanel_admin import AdminMixin
from .webpanel_auth import AuthMixin, derive_session_key
from .webpanel_common import (
    SWITCH_DEFS,
    _PBKDF2_ITERATIONS,
    _SESSION_COOKIE,
    _SESSION_TTL,
    _TEMPLATES_DIR,
    _add_days,
    _aggregate_monthly,
    _env,
    _parse_day,
    _parse_month,
    hash_password,
    logger,
    verify_password,
)
from .webpanel_pages import PagesMixin
from .webpanel_screen import ScreenMixin


if TYPE_CHECKING:
    from .bindings import BindingStore
    from .cookie_pool import CookiePool
    from ..core.manager import MonitorManager
    from ..database.service import DatabaseService


# 兼容re-export：拆分前本模块的模块级对象/函数保持原路径可导入
__all__ = [
    "WebPanel",
    "SWITCH_DEFS",
    "hash_password",
    "verify_password",
    "logger",
    "_TEMPLATES_DIR",
    "_env",
    "_SESSION_COOKIE",
    "_SESSION_TTL",
    "_PBKDF2_ITERATIONS",
    "_add_days",
    "_parse_month",
    "_aggregate_monthly",
    "_parse_day",
]


class WebPanel(AuthMixin, ActionsMixin, PagesMixin, AdminMixin, ScreenMixin,
               ApiV1Mixin):
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
        "analytics": ("/analytics", "观众分析"),
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
        # 会话签名密钥：web_token非空时派生（重启后登录态保留），否则进程随机
        # （重启即全员回到游客；详见webpanel_auth模块docstring）
        self._session_key: bytes = derive_session_key(token)
        self._runner: Optional[web.AppRunner] = None
        # 扫码登录状态（单会话）：{"status", "message", "qr", "result"}
        self._login_state: Optional[dict] = None
        self._login_task: Optional[asyncio.Task] = None
        # 扫码登录归属用户（会话隔离：他人不可查看/取消我的二维码）
        self._login_owner: str = ""
        # 健康检查报告构建器（__main__注入依赖后使用；async fn→dict）
        from typing import Callable as _Callable
        self._health_builder: Optional[_Callable] = None
        # 登录失败计数：key -> (次数, 窗口起点monotonic, 锁定截止monotonic)
        self._login_failures: dict = {}

    async def start(self) -> None:
        """启动HTTP服务"""
        app = web.Application()
        # 健康检查（游客可访问，脱敏只读）
        app.router.add_get("/healthz", self._handle_healthz)
        # 页面（HTMX请求返回内容片段，普通请求返回完整布局；游客只读可浏览）
        for key, (path, _title) in self.PAGES.items():
            app.router.add_get(path, self._make_page_handler(key))
        # 登录/登出
        app.router.add_get("/login", self._handle_login_page)
        app.router.add_post("/web/login", self._action_web_login)
        app.router.add_post("/web/logout", self._action_web_logout)
        # 后台（管理员）
        app.router.add_get("/admin", self._handle_admin_page)
        # 数据大屏（登录用户可访问；独立全屏布局）
        app.router.add_get("/screen", self._handle_screen_page)
        app.router.add_get("/panel/screen", self._handle_screen_data)
        app.router.add_post("/web/admin/user/add", self._action_admin_user_add)
        app.router.add_post("/web/admin/user/password", self._action_admin_user_password)
        app.router.add_post("/web/admin/user/delete", self._action_admin_user_delete)
        app.router.add_post("/web/admin/switch", self._action_admin_switch)
        app.router.add_get("/web/admin/export", self._handle_admin_export)
        app.router.add_get("/web/admin/export/room", self._handle_admin_export_room)
        # 全局开关（前台可改项：登录用户；管理员不受限）
        app.router.add_post("/web/switch/toggle", self._action_switch_toggle)
        # 群独立推送设置（直播页群详情）
        app.router.add_post("/web/room/push_setting", self._action_group_push_setting)
        # 局部/数据
        app.router.add_get("/panel/trend", self._handle_trend)
        app.router.add_get("/panel/wordcloud", self._handle_wordcloud)
        app.router.add_get("/api/status", self._handle_api)
        app.router.add_get("/api/history", self._handle_history)
        # REST API v1（只读，独立鉴权+限速）
        register_api_v1_routes(app, self)
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

    async def _handle_healthz(self, request):
        """健康检查端点（游客可访问，只读脱敏）"""
        from .webpanel_common import logger as _logger
        if self._health_builder is not None:
            try:
                report = await self._health_builder()
            except Exception as e:
                _logger.warning(f"健康检查报告构建失败: {e}")
                report = {"status": "error", "time":
                          datetime.now().isoformat(timespec="seconds")}
        else:
            report = {"status": "unknown"}
        status_code = 200 if report.get("status") == "ok" else 503
        return web.json_response(report, status=status_code)

