# -*- coding: utf-8 -*-
"""
Web状态面板动作路由混入（ActionsMixin）

全部_action_*写操作接口（后台用户/开关管理、直播间与动态订阅、账号
刷新、群推送设置、扫码登录等）。与WebPanel其余职责拆分组合使用
（见webpanel.py）。
"""

import asyncio

from typing import TYPE_CHECKING, Any, Callable, Optional

from aiohttp import web

from .webpanel_common import SWITCH_DEFS, hash_password, logger


if TYPE_CHECKING:
    from .bindings import BindingStore
    from .cookie_pool import CookiePool
    from ..core.manager import MonitorManager
    from ..database.service import DatabaseService


class ActionsMixin:
    """Web面板动作路由混入（写操作/后台动作/扫码登录）"""

    # ---- 以下实例属性由组合后的宿主WebPanel.__init__提供（仅类型标注） ----
    cookie_pool: "CookiePool"
    monitor_manager: "MonitorManager"
    dynamic_manager: Any
    binding_store: "BindingStore"
    db_service: "DatabaseService"
    group_refresher: Any
    # 扫码登录状态（单会话）
    _login_state: Optional[dict]
    _login_task: Optional[asyncio.Task]
    _login_owner: str

    if TYPE_CHECKING:
        # 鉴权能力（AuthMixin提供）
        _require_login: Callable[..., Any]
        _require_admin: Callable[..., Any]
        _requester: Callable[..., Any]
        _is_admin: Callable[..., Any]
        _web_token_ok: Callable[..., Any]

    # ========== 后台动作（仅管理员） ==========

    async def _action_admin_user_add(self, request):
        denied = await self._require_admin(request)
        if denied is not None:
            return denied
        form = await request.post()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        if not username or len(password) < 8:  # OWASP基线最短8位
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
        if not username or len(password) < 8:  # OWASP基线最短8位
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
            switches = await self.db_service.get_push_switches() \
                if self.db_service is not None else {}
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
            current = set(self.binding_store.get_monitored_groups(room_id)) \
                if self.binding_store else set()
            uid = await self.db_service.get_room_uid(room_id) \
                if self.db_service else 0
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
            # 运行时开关门控（与群指令一致）：后台"推送设置-动态监控"关时
            # 不启动轮询（否则"轮询中"与开关状态矛盾、推送被静默丢弃）；
            # 开关打开时_apply_dynamic_switch会按订阅表恢复轮询
            switch_on = True
            if self.db_service is not None and self.dynamic_manager is not None:
                switch_on = await self.db_service.get_push_switch_enabled(
                    "dynamic_monitor", default=self.dynamic_manager.enabled)
            if self.dynamic_manager is not None and switch_on:
                # 用户主动订阅：首轮无条件基线化，防止重新订阅时停订期间的
                # 新动态被一次性补推（进程已存活时参数无效果）
                await self.dynamic_manager.ensure(up_uid, baseline_first=True)
            logger.info(f"面板绑定动态：群{group} UP主{up_uid}"
                        f"（开关{'开' if switch_on else '关-不启轮询'}）")
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
            if removed and self.dynamic_manager is not None \
                    and not self.binding_store.get_dynamic_groups(up_uid):
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
        user, _ = self._requester(request)
        if self._login_task is not None and not self._login_task.done():
            # 已有登录流程：他人发起的不打扰（防二维码窥探/抢登）
            return self._action_response()
        from .bili_login import BiliLoginService

        state = {"status": "starting", "message": "正在启动浏览器…", "qr": None}
        self._login_state = state
        self._login_owner = user

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
        user, is_admin = self._requester(request)
        # 仅登录发起者（或管理员）可取消进行中的扫码流程
        if self._login_owner and user != self._login_owner and not is_admin:
            return self._action_response()
        if self._login_task is not None and not self._login_task.done():
            self._login_task.cancel()
        return self._action_response()

    async def _handle_login_qr(self, request):
        if await self._require_login(request) is not None:
            return web.Response(status=401, text="unauthorized")
        user, is_admin = self._requester(request)
        if self._login_owner and user != self._login_owner and not is_admin:
            return web.Response(status=403, text="forbidden: 登录会话属于其他用户")
        qr = (self._login_state or {}).get("qr")
        if not qr:
            return web.Response(status=404, text="no qrcode")
        return web.Response(body=qr, content_type="image/png")

    async def _handle_login_status(self, request):
        if await self._require_login(request) is not None:
            return web.Response(status=401, text="unauthorized")
        user, is_admin = self._requester(request)
        if self._login_owner and user != self._login_owner and not is_admin:
            return web.json_response({"status": "", "message": ""})
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
