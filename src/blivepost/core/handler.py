# -*- coding: utf-8 -*-
"""
直播事件处理器
"""

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

from ..config.settings import Settings, MessageSettings
from ..models.live import LiveStats, TempData
from ..services.push import PushService
from ..utils.logger import get_logger
from ..utils.timeutil import dt_to_ms, now_ms
from ..vendor.importer import import_blivedm


def _parse_live_time(text: str) -> Optional[int]:
    """
    解析REST接口的开播时间（"YYYY-MM-DD HH:mm:ss"）为13位毫秒时间戳

    未开播等场景返回"0000-00-00 00:00:00"或空值，解析失败返回None。
    """
    if not text or text.startswith("0000"):
        return None
    try:
        return dt_to_ms(datetime.strptime(text, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return None


if TYPE_CHECKING:
    import blivedm
    from blivedm.models import web


logger = get_logger("handler")


class BaseEventHandler:
    """
    基础直播事件处理器

    负责日志记录、消息推送、直播统计与数据库落库的全部逻辑。
    事件回调接收的message均为blivedm解析后的消息对象（由patch.py
    中注册的命令回调完成解析）。
    """

    # 上舰等级映射
    GUARD_LEVEL_MAP = {
        1: "总督",
        2: "提督",
        3: "舰长"
    }

    def __init__(
        self,
        settings: Settings,
        push_service: PushService,
        stats: LiveStats,
        temp_data: TempData,
        room_id: str,
        db_service: Optional[Any] = None
    ):
        """
        初始化事件处理器

        Args:
            settings: 应用配置
            push_service: 推送服务
            stats: 直播统计数据
            temp_data: 临时数据
            room_id: 直播间ID
            db_service: 数据库存储服务（可选）
        """
        self.settings = settings
        self.message_settings: MessageSettings = settings.message
        self.push_service = push_service
        self.stats = stats
        self.temp_data = temp_data
        self.room_id_str = str(room_id)
        try:
            self.room_id_int = int(room_id)
        except (TypeError, ValueError):
            self.room_id_int = 0
        self.db_service = db_service

        # 直播状态
        self.is_live = False

    def _async_save(self, coro) -> None:
        """
        异步任务的辅助方法（在事件循环线程内创建后台任务）

        有db_service时调度协程（落库/推送），否则安全丢弃。
        后台任务挂done回调记日志——否则异常只在GC时以
        "exception was never retrieved"出现，排障无迹可寻。
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.error("异步任务调度失败: 当前无运行中的事件循环")
            coro.close()
            return
        if self.db_service:
            task = asyncio.create_task(coro)

            def _log_exc(t: asyncio.Task) -> None:
                if not t.cancelled() and t.exception() is not None:
                    logger.error(f"后台落库任务失败: {t.exception()}")

            task.add_done_callback(_log_exc)
        else:
            coro.close()

    def _on_heartbeat(
        self,
        client: "blivedm.BLiveClient",
        message: "web.HeartbeatMessage"
    ) -> None:
        """心跳回调"""
        if message.popularity != 1 and self.is_live:
            if self.temp_data.should_update_popular(message.popularity):
                logger.info(f"当前人气值：{message.popularity}")

    def _on_watched_change(
        self,
        client: "blivedm.BLiveClient",
        message: "web.WatchedChange"
    ) -> None:
        """观看人数变更"""
        if self.is_live:
            if self.temp_data.should_update_watched(message.num):
                logger.info(f"{message.num}人看过")

    def _on_not_login(
        self,
        client: "blivedm.BLiveClient",
        info: "web.LoginnoticeCmd"
    ) -> None:
        """未登录通知：停掉本地连接并上报（主进程暂停重建并向绑定群提醒）"""
        logger.error("账号未登录或登录已过期")
        try:
            self.push_service.notify_not_login(self.room_id_int)
        except Exception as e:
            logger.error(f"凭证失效上报失败: {e}")
        client.stop()

    def _on_danmaku(
        self,
        client: "blivedm.BLiveClient",
        message: "web.DanmakuMessage"
    ) -> None:
        """弹幕回调"""
        # 普通弹幕
        if message.dm_type == 0 and message.msg_type != 2:
            logger.info(f"{message.uname}[{message.uid}]：{message.msg}")
            self.stats.danmaku_count += 1
            if self.db_service:
                self._async_save(
                    self.db_service.save_danmaku(
                        room_id=self.room_id_int,
                        uid=message.uid,
                        uname=message.uname,
                        msg=message.msg,
                        dm_type=0
                    )
                )

        # 表情弹幕
        elif message.dm_type == 1 and "official" not in message.emoticon_options.get("emoticon_unique", ""):
            url = message.emoticon_options.get("url", "")
            logger.info(f"{message.uname}[{message.uid}]：[表情]{message.msg}({url})")
            self.stats.danmaku_count += 1
            if self.db_service:
                self._async_save(
                    self.db_service.save_danmaku(
                        room_id=self.room_id_int,
                        uid=message.uid,
                        uname=message.uname,
                        msg=message.msg,
                        dm_type=1,
                        emoticon_url=url
                    )
                )

    def _on_gift(
        self,
        client: "blivedm.BLiveClient",
        message: "web.GiftMessage"
    ) -> None:
        """礼物回调"""
        gift_price = message.price * message.num
        if gift_price == 0:
            return

        coin_type = "金" if message.coin_type == "gold" else "银"
        logger.info(
            f"{message.uname}[{message.uid}] 赠送{message.gift_name}x{message.num} "
            f"（{coin_type}瓜子x{gift_price}）"
        )

        if message.coin_type == "gold":
            if self.is_live:
                self.stats.add_gift(gift_price, message.coin_type)

            if self.db_service:
                self._async_save(
                    self.db_service.save_gift(
                        room_id=self.room_id_int,
                        uid=message.uid,
                        uname=message.uname,
                        gift_id=message.gift_id,
                        gift_name=message.gift_name,
                        num=message.num,
                        price=message.price,
                        coin_type=message.coin_type
                    )
                )

    def _on_user_toast_v2(
        self,
        client: "blivedm.BLiveClient",
        message: "web.UserToastV2Message"
    ) -> None:
        """上舰回调"""
        gift_name = self.GUARD_LEVEL_MAP.get(message.guard_level, "未知")
        logger.info(f"{message.username}[{message.uid}] 购买{gift_name}x{message.num}")

        if self.is_live:
            self.stats.add_ship(message.price)

        if self.db_service:
            self._async_save(
                self.db_service.save_guard(
                    room_id=self.room_id_int,
                    uid=message.uid,
                    uname=message.username,
                    guard_level=message.guard_level,
                    gift_name=gift_name,
                    price=message.price,
                    num=message.num
                )
            )

    def _on_super_chat(
        self,
        client: "blivedm.BLiveClient",
        message: "web.SuperChatMessage"
    ) -> None:
        """醒目留言回调"""
        logger.info(f"醒目留言 ¥{message.price} {message.uname}[{message.uid}]：{message.message}")

        if self.is_live:
            self.stats.add_sc(message.price)

        if self.db_service:
            self._async_save(
                self.db_service.save_super_chat(
                    room_id=self.room_id_int,
                    uid=message.uid,
                    uname=message.uname,
                    price=message.price,
                    message=message.message
                )
            )

    def _on_live_start(
        self,
        client: "blivedm.BLiveClient",
        info: "web.LiveStart"
    ) -> None:
        """开播回调"""
        if self.is_live:
            return

        logger.info("直播开始")
        self.is_live = True
        # 先清零统计再记开播时间（reset会将start_time置None，顺序颠倒会0时长）
        self.stats.reset()
        self.stats.start_time = now_ms()
        self.temp_data = TempData()

        # 开播推送与落库（异步调度，同步API请求在线程中执行，不阻塞事件循环）
        self._async_save(self._push_live_start())

    async def _push_live_start(self) -> None:
        """异步开播推送"""
        try:
            room_info = await self._get_room_info()
            if room_info:
                # 开播时间回填：轮询/校正检测到的开播往往晚于实际开播，
                # 用REST接口的开播时间校正（取更早值，时长才准确）
                parsed = _parse_live_time(getattr(room_info, "live_time", ""))
                if parsed and (self.stats.start_time is None
                               or parsed < self.stats.start_time):
                    self.stats.start_time = parsed
                self.push_service.push_live_start(room_info)
                if self.db_service:
                    self._async_save(
                        self.db_service.save_live_start(
                            room_id=self.room_id_int,
                            title=room_info.title,
                            cover=room_info.cover
                        )
                    )
        except Exception as e:
            logger.error(f"开播推送失败: {e}")

    def _on_live_preparing(
        self,
        client: "blivedm.BLiveClient",
        info: "web.LivePreparing"
    ) -> None:
        """下播回调"""
        if not self.is_live:
            logger.info("直播结束")
            return

        logger.info("直播结束")
        logger.info(
            f"统计 - 弹幕: {self.stats.danmaku_count}, "
            f"礼物收入: {self.stats.gold_count}千瓜子, "
            f"上舰收入: {self.stats.ship_count}千瓜子, "
            f"SC收入: {self.stats.sc_count}元"
        )

        # 下播推送与落库（异步调度，同步API请求在线程中执行，不阻塞事件循环）
        self._async_save(self._push_live_end())

        self.is_live = False
        self.stats.end_time = now_ms()

    async def _push_live_end(self) -> None:
        """异步下播推送"""
        try:
            room_info = await self._get_room_info()
            if room_info:
                self.push_service.push_live_end(room_info, self.stats)
                if self.db_service:
                    self._async_save(
                        self.db_service.save_live_end(
                            room_id=self.room_id_int,
                            stats=self.stats,
                            title=room_info.title,
                            cover=room_info.cover
                        )
                    )
        except Exception as e:
            logger.error(f"下播推送失败: {e}")

    async def _get_room_info(self):
        """获取当前直播间信息（同步API请求在线程中执行，开播/下播推送用）"""
        api = getattr(self.push_service, "bilibili_api", None)
        if not api:
            return None
        return await asyncio.to_thread(api.get_room_info, self.room_id_str)

    def _on_anchor_start(
        self,
        client: "blivedm.BLiveClient",
        message: "web.AnchorStart"
    ) -> None:
        """天选抽奖回调"""
        logger.info(f"天选抽奖：礼物:{message.award_name}，弹幕:{message.danmu}，要求:{message.require_text}")

        self.push_service.push_anchor(
            self.room_id_str, message.award_name, message.danmu, message.require_text
        )

        if self.db_service:
            self._async_save(
                self.db_service.save_anchor(
                    room_id=self.room_id_int,
                    award_name=message.award_name,
                    danmu=message.danmu,
                    require_text=message.require_text
                )
            )

    def _on_redpocket_start(
        self,
        client: "blivedm.BLiveClient",
        message: "web.RedPocketStart"
    ) -> None:
        """红包抽奖回调"""
        logger.info(f"红包抽奖：发送人:{message.sender_name}，总价值:{message.total_price}电池")

        if self.is_live:
            self.stats.add_red_pocket(message.total_price)

        if self.db_service:
            self._async_save(
                self.db_service.save_red_pocket(
                    room_id=self.room_id_int,
                    sender_uid=message.sender_uid,
                    sender_name=message.sender_name,
                    total_price=message.total_price
                )
            )

    def _on_guard_achievement(
        self,
        client: "blivedm.BLiveClient",
        info: "web.GuardAchievementRoom"
    ) -> None:
        """大航海成就回调"""
        if info.anchor_guard_achieve_level == 0:
            return

        content = (
            info.first_line_content.replace("<%", "").replace("%>", "") +
            info.second_line_content.replace("<%", "").replace("%>", "")
        )
        logger.info(f"大航海成就：{content}")

        self.push_service.push_guard_achievement(self.room_id_str, content)
        if self.db_service:
            self._async_save(
                self.db_service.save_guard_achievement(
                    room_id=self.room_id_int,
                    achieve_level=info.anchor_guard_achieve_level,
                    content=content
                )
            )

    def _on_cut_off(
        self,
        client: "blivedm.BLiveClient",
        info: "web.CutOff"
    ) -> None:
        """直播切断回调"""
        logger.warning(f"直播被切断，原因：{info.msg}")
        self.push_service.push_cut_off(self.room_id_str, info.msg)
        if self.db_service:
            self._async_save(
                self.db_service.save_cut_off(
                    room_id=self.room_id_int,
                    msg=info.msg
                )
            )

    def _on_receive_warning(
        self,
        client: "blivedm.BLiveClient",
        info: "web.Warned"
    ) -> None:
        """收到警告回调"""
        logger.warning(f"收到警告，原因：{info.msg}")
        self.push_service.push_warning(self.room_id_str, info.msg)
        if self.db_service:
            self._async_save(
                self.db_service.save_warning(
                    room_id=self.room_id_int,
                    msg=info.msg
                )
            )

    def _on_like_update(
        self,
        client: "blivedm.BLiveClient",
        info: "web.LikeUpdate"
    ) -> None:
        """点赞更新回调"""
        if self.is_live:
            if self.temp_data.should_update_like(info.click_count):
                logger.info(f"当前点赞总数: {info.click_count}")

    def _on_pk_start(
        self,
        client: "blivedm.BLiveClient",
        info: "web.PKStart"
    ) -> None:
        """PK开始回调"""
        logger.info(f"与主播{info.uname}[{info.uid}](房间号{info.room_id})的PK即将开始")

    def _on_room_block(
        self,
        client: "blivedm.BLiveClient",
        info: "web.RoomBlock"
    ) -> None:
        """禁言回调"""
        logger.info(f"{info.uname}[{info.uid}]被禁言")
        self.push_service.push_room_block(self.room_id_str, info.uname, info.uid)
        if self.db_service:
            self._async_save(
                self.db_service.save_ban(
                    room_id=self.room_id_int,
                    uid=info.uid,
                    uname=info.uname
                )
            )

    def _on_admin_entrance(
        self,
        client: "blivedm.BLiveClient",
        info: "web.AdminEntrance"
    ) -> None:
        """设置房管回调"""
        logger.info(f"[{info.uid}]被设为房管")
        self.push_service.push_admin_change(self.room_id_str, info.uid, revoked=False)
        if self.db_service:
            self._async_save(
                self.db_service.save_admin_set(
                    room_id=self.room_id_int,
                    uid=info.uid
                )
            )

    def _on_admin_revoke(
        self,
        client: "blivedm.BLiveClient",
        info: "web.AdminRevoke"
    ) -> None:
        """撤销房管回调"""
        logger.info(f"[{info.uid}]被撤销房管")
        self.push_service.push_admin_change(self.room_id_str, info.uid, revoked=True)
        if self.db_service:
            self._async_save(
                self.db_service.save_admin_revoke(
                    room_id=self.room_id_int,
                    uid=info.uid
                )
            )

    def _on_room_change(
        self,
        client: "blivedm.BLiveClient",
        info: "web.RoomChange"
    ) -> None:
        """房间信息变更回调"""
        logger.info(f"直播间信息更新：标题: {info.title}, 分区: {info.parent_area_name}-{info.area_name}")
        self.push_service.push_room_change(
            self.room_id_str, info.title,
            info.parent_area_name, info.area_name
        )
        if self.db_service:
            self._async_save(
                self.db_service.save_room_change(
                    room_id=self.room_id_int,
                    title=info.title,
                    parent_area_id=info.parent_area_id,
                    parent_area_name=info.parent_area_name,
                    area_id=info.area_id,
                    area_name=info.area_name
                )
            )


# 兼容旧导出名：事件处理逻辑已统一到BaseEventHandler
LiveEventHandler = BaseEventHandler


def create_handler_with_base(
    settings: Settings,
    push_service: PushService,
    stats: LiveStats,
    temp_data: TempData,
    room_id: str,
    db_service: Optional[Any] = None
) -> Any:
    """
    创建带有blivedm.BaseHandler的事件处理器

    动态创建一个同时继承BaseEventHandler与blivedm.BaseHandler的类。
    BaseEventHandler在前，保证事件回调使用本项目实现的版本
    （包含推送、统计与落库），而消息分发handle使用blivedm的版本。

    Args:
        settings: 应用配置
        push_service: 推送服务
        stats: 直播统计数据
        temp_data: 临时数据
        room_id: 直播间ID
        db_service: 数据库存储服务（可选）

    Returns:
        处理器实例
    """
    blivedm = import_blivedm()

    class _Handler(BaseEventHandler, blivedm.BaseHandler):  # type: ignore[name-defined]
        def __init__(self):
            BaseEventHandler.__init__(
                self,
                settings=settings,
                push_service=push_service,
                stats=stats,
                temp_data=temp_data,
                room_id=room_id,
                db_service=db_service
            )

    return _Handler()
