# -*- coding: utf-8 -*-
"""
Web状态面板后台混入（AdminMixin）

管理后台页面与数据导出（用户管理/推送开关/SQLite导出打包）。
与WebPanel其余职责拆分组合使用（见webpanel.py）。
"""

import asyncio
import io
import tempfile
import zipfile

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from aiohttp import web

from .webpanel_common import SWITCH_DEFS, logger


if TYPE_CHECKING:
    from ..database.service import DatabaseService


class AdminMixin:
    """Web面板后台混入（管理页面与数据导出，仅管理员）"""

    # ---- 以下实例属性由组合后的宿主WebPanel.__init__提供（仅类型标注） ----
    db_service: "DatabaseService"
    # 页面注册表：key -> (路径, 标题)（由宿主WebPanel定义）
    PAGES: dict

    if TYPE_CHECKING:
        # 鉴权能力（AuthMixin提供）
        _is_admin: Callable[..., Any]
        _web_token_ok: Callable[..., Any]
        _current_user: Callable[..., Any]
        _require_admin: Callable[..., Any]
        # 渲染能力（PagesMixin提供）
        _render: Callable[..., Any]
        _html_response: Callable[..., Any]

    # ========== 后台页面（仅管理员） ==========

    def _require_admin_page(self, request) -> Optional[web.HTTPFound]:
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

    # ========== 数据导出（仅SQLite存储） ==========

    async def _handle_admin_export(self, request):
        """导出数据：主库+全部房间子库+动态子库打包zip下载（仅SQLite）。

        默认导出脱敏副本（VACUUM INTO一致性快照+清除cookie_account凭证，
        防一个zip即泄露全部B站账号凭证）；?credentials=1由管理员显式导出原始库。
        """
        denied = await self._require_admin(request)
        if denied is not None:
            return denied
        sqlite_path = self._sqlite_path()
        if sqlite_path is None:
            return web.Response(status=409, text="仅SQLite存储支持导出")
        db_path = Path(sqlite_path)
        if not db_path.exists():
            return web.Response(status=404, text="数据库文件不存在")
        data_dir = db_path.parent
        include_creds = request.query.get("credentials") == "1"
        buf = await asyncio.get_running_loop().run_in_executor(
            None, self._build_export_zip, db_path, data_dir, include_creds)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = "raw" if include_creds else "sanitized"
        return web.Response(
            body=buf.getvalue(), content_type="application/zip",
            headers={"Content-Disposition":
                     f'attachment; filename="blivepost_export_{suffix}_{stamp}.zip"'})

    def _sqlite_path(self) -> Optional[str]:
        """当前主库文件路径（非SQLite配置返回None）"""
        getter = getattr(self.db_service, "sqlite_path", None) \
            if self.db_service is not None else None
        return getter() if callable(getter) else None

    @staticmethod
    def _sanitized_snapshot(src: Path, dst: Path) -> Optional[Path]:
        """VACUUM INTO一致性快照并清除cookie_account凭证表；失败返回None跳过"""
        import sqlite3
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            con = sqlite3.connect(str(src))
            try:
                con.execute("VACUUM INTO ?", (str(dst),))
            finally:
                con.close()
            con = sqlite3.connect(str(dst))
            try:
                tables = {row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if "cookie_account" in tables:
                    con.execute("DELETE FROM cookie_account")
                    con.commit()
            finally:
                con.close()
            return dst
        except Exception as e:
            logger.warning(f"导出快照失败({src.name}): {e}，该库跳过")
            return None

    @classmethod
    def _build_export_zip(cls, db_path: Path, data_dir: Path,
                          include_credentials: bool = False) -> "io.BytesIO":
        """打包主库+rooms/+dynamics/全部sqlite文件。

        include_credentials=False时经VACUUM INTO快照（写入中的库得到
        一致视图，规避直接zip热文件的丢页风险）并清除凭证表。
        """
        buf = io.BytesIO()
        with tempfile.TemporaryDirectory(prefix="blivepost_export_") as tmp:
            tmp_dir = Path(tmp)
            sources: list = [("data/" + db_path.name, db_path)]
            for sub in ("rooms", "dynamics"):
                root = data_dir / sub
                if not root.exists():
                    continue
                sources += [
                    (str(f.relative_to(data_dir).as_posix()), f)
                    for f in sorted(root.rglob("*.db"))
                ]
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for arcname, src in sources:
                    target = src
                    if not include_credentials:
                        target = cls._sanitized_snapshot(
                            src, tmp_dir / arcname.replace("/", "_"))
                    if target is not None:
                        zf.write(target, arcname=arcname)
        return buf
