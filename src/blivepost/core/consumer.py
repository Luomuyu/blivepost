# -*- coding: utf-8 -*-
"""
跨进程队列消费者（主进程）

消费子进程发来的推送与落库请求（子进程侧见room_process.py）：
推送经PushService按绑定关系路由（优先富媒体），落库集中写入数据库。
"""

import asyncio
import queue

from typing import TYPE_CHECKING, Optional

from ..services.push import PushService, _supports_kwarg
from ..utils.logger import get_logger


if TYPE_CHECKING:
    from ..database import DatabaseService
    from .manager import MonitorManager


logger = get_logger("manager")


class PushQueueConsumer:
    """
    跨进程队列消费者（主进程）

    消费子进程发来的推送与落库请求：推送经PushService按绑定关系
    路由（优先富媒体），落库集中写入数据库。
    """

    def __init__(
        self,
        queue,
        push_service: PushService,
        db_service: Optional["DatabaseService"] = None,
        monitor_manager: Optional["MonitorManager"] = None,
        cookie_pool=None,
        admin_openids: Optional[list] = None,
        main_admin_openids: Optional[list] = None,
        binding_store=None,
        switch_defaults: Optional[dict] = None
    ):
        """
        初始化消费者

        Args:
            queue: 跨进程队列（MonitorManager.push_queue）
            push_service: 主进程推送服务
            db_service: 数据库存储服务
            monitor_manager: 监控管理器（处理子进程上报时暂停/重建用）
            cookie_pool: Cookie账号池（凭证失效轮转用）
            admin_openids: 管理员openid列表（私聊提醒用）
            switch_defaults: 全局推送开关config默认值（运行时以DB为准，缺行回落）
        """
        self._queue = queue
        self.push_service = push_service
        self.db_service = db_service
        self.monitor_manager = monitor_manager
        self.cookie_pool = cookie_pool
        self.admin_openids = [str(o) for o in (admin_openids or [])]
        # 主管理员：接收Cookie失效等敏感通知，未配置时回落全部管理员
        self.main_admin_openids = [str(o) for o in (main_admin_openids or [])]
        self.binding_store = binding_store
        self.switch_defaults = switch_defaults or {}
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    async def _switch_on(self, name: str) -> bool:
        """全局推送开关运行时状态（DB优先，缺行/无DB回落config默认）"""
        default = bool(self.switch_defaults.get(name, False))
        if self.db_service is None:
            return default
        try:
            return await self.db_service.get_push_switch_enabled(name, default)
        except Exception:
            return default

    async def _allowed_groups(self, room_id, switch: str) -> Optional[list]:
        """按群独立推送设置过滤收群（行存在用行值，缺省回落全局开关）

        Returns:
            Optional[list]: 允许收推送的群列表；无绑定存储时None（回落全局开关判定）
        """
        if self.binding_store is None:
            return None
        try:
            rid = int(room_id) if room_id is not None else 0
        except (TypeError, ValueError):
            rid = 0
        groups = self.binding_store.get_monitored_groups(rid)
        if not groups:
            return []
        global_on = await self._switch_on(switch)
        rows: dict = {}
        if self.db_service is not None:
            try:
                rows = await self.db_service.get_group_push_settings(rid)
            except Exception:
                rows = {}
        return [g for g in groups if (rows.get(g) or {}).get(switch, global_on)]

    def start(self) -> None:
        """启动消费循环（需在事件循环内调用）"""
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self.run(), name="push-queue-consumer")
            logger.info("子进程消息消费者已启动")

    async def stop(self) -> None:
        """停止消费（置停止标志，等待循环在轮询间隙退出）"""
        self._stopping = True
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
            if not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None

    async def run(self) -> None:
        """消费主循环（带超时轮询，避免线程无限阻塞在队列上）"""
        while True:
            try:
                item = await asyncio.to_thread(self._queue.get, True, 1)
            except queue.Empty:
                if self._stopping:
                    break
                continue
            if item is None:
                break
            try:
                await self._handle(item)
            except Exception as e:
                logger.error(f"处理子进程消息失败: {e}")

    async def _handle(self, item: dict) -> None:
        """处理单条队列消息"""
        kind = item.get("type")

        if kind == "push_live_start":
            # 开播推送：主进程侧执行 Markdown+按钮→富媒体→文本 降级链；
            # 当天首次开播提醒的群自动艾特全体（每群每自然日1次配额）
            at_all_groups = await self._acquire_at_all_groups(item.get("room_id"))
            self.push_service.push_live_start_from_parts(
                content=item.get("content", ""),
                media=item.get("media"),
                room_id=item.get("room_id"),
                keyboard=item.get("keyboard"),
                at_all_groups=at_all_groups,
            )
            # 调度可观测：真机排障依赖（发送结果在bot层降级链日志）
            n_groups = len(self.push_service.resolve_push_groups(item.get("room_id")))
            logger.info(f"开播推送已调度：房间{item.get('room_id')} → {n_groups}群"
                        f"（艾特全体{len(at_all_groups or [])}群）")

        elif kind == "push":
            switch = item.get("switch")
            room_id = item.get("room_id")
            media = item.get("media")
            content = item.get("content", "")
            keyboard = item.get("keyboard")
            # 群独立设置过滤（行值优先，缺行回落全局开关）；无绑定存储时回落全局开关判定
            allowed = await self._allowed_groups(room_id, switch) if switch else None
            if allowed is not None and not allowed:
                logger.debug(f"开关{switch}下房间{room_id}无启用推送的群，丢弃")
                return
            drop = allowed is None and switch and not await self._switch_on(switch)
            if drop:
                logger.debug(f"全局推送开关 {switch} 已关闭，丢弃推送（房间{room_id}）")
                return
            with self.push_service._groups(allowed):
                if media:
                    self.push_service.push_media_message(media, content, room_id=room_id,
                                                         keyboard=keyboard)
                else:
                    self.push_service.send_message(content, room_id=room_id,
                                                   keyboard=keyboard)

        elif kind == "not_login":
            await self._handle_not_login(item.get("room_id"))

        elif kind == "dynamic":
            if not await self._switch_on("dynamic_monitor"):
                # INFO级可见性：开关关闭期间新动态被丢弃应可追溯（真机反馈
                # "未推送"曾因debug级日志无迹可寻）
                logger.info("全局推送开关 dynamic_monitor 已关闭，丢弃动态推送"
                            f"（dynamic_id={item.get('dynamic_id')}）")
                return
            await self._handle_dynamic(item)

        elif kind == "up_live":
            if not await self._switch_on("dynamic_monitor"):
                logger.info("全局推送开关 dynamic_monitor 已关闭，丢弃UP开播提醒"
                            f"（up_uid={item.get('up_uid')}）")
                return
            await self._handle_up_live(item)

        elif kind == "dynamic_alert":
            # 动态轮询子进程连续异常/空响应（风控嫌疑）→ 私聊管理员
            up_uid = item.get("up_uid")
            consecutive = item.get("consecutive", 0)
            await self._notify_admins(
                f"⚠️ UP主 {up_uid} 的动态接口已连续{consecutive}轮异常/空响应"
                f"（约{consecutive * 90 // 60}分钟），疑似被风控或接口变更，"
                f"动态推送可能中断，请检查。"
            )

        elif kind == "db":
            method = getattr(self.db_service, item["method"], None) if self.db_service else None
            if method is None:
                logger.error(f"落库方法不存在: {item['method']}")
                return
            await method(**item["kwargs"])

        else:
            logger.warning(f"未知队列消息类型: {kind}")

    async def _handle_not_login(self, room_id) -> None:
        """
        处理子进程上报的登录凭证失效

        标记账号失效并私聊提醒管理员（不在群里发送），随后从账号池
        轮转其他账号重建监控；池中无可用账号时暂停监控。
        """
        rid = int(room_id) if room_id is not None else 0
        uid = self.monitor_manager.get_cookie_uid(rid) if self.monitor_manager else 0
        logger.error(f"房间 {rid} 上报B站登录凭证失效 (账号uid={uid})")

        if self.cookie_pool is not None and uid:
            await self.cookie_pool.report_invalid(uid, "弹幕连接报告登录失效")

        await self._notify_admins(
            f"⚠️ 账号 {uid} 的B站登录凭证已失效（直播间{rid}）", sensitive=True
        )

        if self.monitor_manager is None:
            return

        # 移除使用失效凭证的监控进程
        await self.monitor_manager.remove(rid)

        # 账号池还有可用账号则轮转重建，否则暂停
        if self.cookie_pool is not None and self.cookie_pool.active_accounts():
            await self.monitor_manager.ensure(rid)
            logger.info(f"直播间 {rid} 已轮转账号重启监控")
            warn = self.cookie_pool.warn_if_needed()
            if warn:
                await self._notify_admins(f"⚠️ {warn}", sensitive=True)
            return

        await self.monitor_manager.suspend(rid, reason="登录凭证失效且账号池无可用账号")
        await self._notify_admins(
            f"账号池无可用账号，直播间{rid}的监控已暂停。\n"
            f"请私聊机器人发送 /登录 添加账号后，@机器人 发送 /监控 {rid} 恢复。"
        )

    def _receivable(self, groups: list) -> list:
        """过滤未开启消息接收的群（与房间推送路径口径一致，配额判定同源）"""
        contact_store = getattr(self.push_service, "contact_store", None)
        if contact_store is None or not groups:
            return list(groups)
        return contact_store.filter_receivable_groups(groups)

    async def _handle_dynamic(self, item: dict) -> None:
        """处理子进程上报的新动态：推送到所有订阅该UP主的群"""
        up_uid = item.get("up_uid")
        dynamic_id = item.get("dynamic_id", "")
        author = item.get("author") or (
            self.binding_store.get_dynamic_up_name(up_uid)
            if self.binding_store else "") or f"UP主{up_uid}"
        from datetime import datetime as _dt
        # pub_ts防御性float：接口/历史队列项可能为字符串（真机2026-11-10
        # fromtimestamp(str)抛TypeError致动态推送失败）
        try:
            pub_ts = float(item.get("pub_ts") or 0)
        except (TypeError, ValueError):
            pub_ts = 0.0
        pub_time = _dt.fromtimestamp(pub_ts).strftime("%Y-%m-%d %H:%M") if pub_ts else "刚刚"

        # 回填UP主昵称（与已存一致时跳过写库）
        if (self.binding_store is not None and item.get("author")
                and self.binding_store.get_dynamic_up_name(up_uid) != item["author"]):
            await self.binding_store.update_dynamic_up_name(up_uid, item["author"])

        content = (
            f"{author} 发布新动态\n"
            f"时间：{pub_time}\n"
            f"链接：https://www.bilibili.com/opus/{dynamic_id}"
        )
        summary = (item.get("summary") or "").strip()
        if summary:
            content += f"\n内容：{summary}"
        groups = self._receivable(
            self.binding_store.get_dynamic_groups(up_uid) if self.binding_store else [])
        if not groups:
            logger.info(f"UP主 {up_uid} 的新动态无订阅群，跳过推送")
            return
        bot = getattr(self.push_service, "bot_client", None)
        if bot is None:
            logger.warning("机器人未启动，动态推送失败")
            return
        # 配图优先级（2026-11-10用户决策）：本地渲染图（b64，经群files
        # 二进制直传投递）> 动态原图URL（平台转存）> 纯文本广播。
        # 注意：push_media_data/push_media/broadcast均为同步入口（内部
        # create_task），不可await
        image_b64 = (item.get("image_b64") or "").strip()
        image = (item.get("image") or "").strip()
        pushed = False
        if image_b64:
            import base64 as _b64mod
            try:
                data = _b64mod.b64decode(image_b64)
            except Exception:
                data = None
            push_data = getattr(bot, "push_media_data", None)
            if data and push_data is not None:
                pushed = push_data(data, content, groups)
        if not pushed and image:
            bot.push_media(image, content, groups)
        elif not pushed:
            bot.broadcast(groups, content)
        logger.info(f"UP主 {up_uid} 新动态已推送到 {len(groups)} 个群"
                    f"（图: {item.get('image_source') or ('URL' if image else '无')}）")

    async def _handle_up_live(self, item: dict) -> None:
        """处理开播提醒：推送到所有订阅该UP主动态的群（当天首次开播提醒的群艾特全体）"""
        up_uid = item.get("up_uid")
        author = (self.binding_store.get_dynamic_up_name(up_uid)
                  if self.binding_store else "") or f"UP主{up_uid}"
        title = item.get("title") or ""
        room_id = item.get("room_id") or 0

        content = f"{author} 开播啦！\n标题：{title}\n链接：https://live.bilibili.com/{room_id}"
        # 先过滤收群再判配额：未开接收的群不推送也不占用@全体当日配额
        groups = self._receivable(
            self.binding_store.get_dynamic_groups(up_uid) if self.binding_store else [])
        if not groups:
            return
        bot = getattr(self.push_service, "bot_client", None)
        if bot is None:
            logger.warning("机器人未启动，开播提醒推送失败")
            return
        at_all_groups = await self._acquire_at_all_for_groups(
            groups, room_id=int(room_id or 0))
        # 自定义bot可能不支持at_all_groups参数（鸭子类型兼容），不支持则原样发送
        if at_all_groups and _supports_kwarg(bot.broadcast, "at_all_groups"):
            bot.broadcast(groups, content, at_all_groups=at_all_groups)
        else:
            bot.broadcast(groups, content)  # 同步入口，不可await
        logger.info(f"UP主 {up_uid} 开播提醒已推送到 {len(groups)} 个群")

    async def _acquire_at_all_groups(self, room_id) -> Optional[list]:
        """房间开播提醒的艾特全体配额（解析推送目标群后判定；无DB/无群返回None）"""
        if self.db_service is None or room_id is None:
            return None
        groups = self.push_service.resolve_push_groups(room_id)
        return await self._acquire_at_all_for_groups(groups, room_id=room_id)

    async def _acquire_at_all_for_groups(self, groups,
                                         room_id: int = 0) -> Optional[list]:
        """给定群列表的艾特全体配额与角色门控。

        流程：peek当日未用群（不写库，含at_all模式过滤：off跳过/rooms按
        房间过滤）→ 按机器人群内角色过滤（群主/管理员才放行；普通成员跳过
        且不占配额；角色未知=接口不可用时放行，靠发送失败降级兜底）→ 仅对
        放行群登记配额（mark）。
        发送失败由bot层经at_all_refund退还当日配额。
        """
        if self.db_service is None or not groups:
            return None
        try:
            candidates = await self.db_service.acquire_at_all_groups(
                list(groups), room_id=int(room_id or 0), mark=False)
            if not candidates:
                return None
            permitted = await self._at_all_role_permitted(candidates)
            if not permitted:
                return None
            marked = await self.db_service.acquire_at_all_groups(permitted)
            if marked:
                logger.info(f"开播提醒艾特全体（当日配额）：{len(marked)}个群")
            return marked or None
        except Exception as e:
            logger.warning(f"艾特全体配额判定失败，本次不艾特: {e}")
            return None

    async def _at_all_role_permitted(self, groups: list) -> list:
        """角色门控：机器人须为群主(owner)或管理员(admin)才艾特全体。

        get_group_role不可用（自定义bot无该接口/白名单外/网络失败）返回
        None时放行尝试——降级链兜底；member角色的群直接跳过（不发起、不占配额）。
        """
        bot = getattr(self.push_service, "bot_client", None)
        get_role = getattr(bot, "get_group_role", None)
        if get_role is None:
            return list(groups)
        permitted = []
        for g in groups:
            try:
                role = await get_role(g)
            except Exception as e:
                logger.debug(f"查询群角色异常(群{g[:6]}…): {e}")
                role = None
            if role in (None, "owner", "admin"):
                permitted.append(g)
            else:
                logger.info(f"群{g[:6]}… 机器人为普通成员({role})，跳过艾特全体")
        return permitted

    async def _notify_admins(self, content: str, sensitive: bool = False) -> None:
        """
        私聊提醒管理员（Cookie相关敏感信息不进群）

        Args:
            content: 通知内容
            sensitive: 敏感通知发主管理员（未配置主管理员时发全部管理员）
        """
        targets = self.admin_openids
        if sensitive and self.main_admin_openids:
            targets = self.main_admin_openids
        elif not targets and self.main_admin_openids:
            # openids未配置但配了主管理员：普通通知回退主管理员（避免告警仅落日志）
            targets = self.main_admin_openids
        if not targets:
            logger.info(f"(无管理员配置，仅记录) {content}")
            return
        bot = getattr(self.push_service, "bot_client", None)
        if bot is None:
            logger.warning(f"机器人未启动，无法私聊提醒管理员: {content}")
            return
        await bot.notify_admins(targets, content)
