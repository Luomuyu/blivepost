# -*- coding: utf-8 -*-
"""
数据库存储服务 - 直播间事件写入（EventWriterMixin）
"""

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from .models import (
    DanmakuRecord, GiftRecord, SuperChatRecord, GuardRecord,
    AnchorRecord, RedPocketRecord, GuardAchievementRecord,
    SystemEventRecord, RoomChangeRecord, LiveStatusRecord, EventType
)
from ..utils.logger import get_logger
from ..utils.timeutil import now_ms

if TYPE_CHECKING:
    from .manager import DatabaseManager
    from ..models.live import LiveStats


logger = get_logger("db_service")


class EventWriterMixin:
    """直播间事件写入混入（事件数据经房间子库路由落库；组合进DatabaseService）"""

    if TYPE_CHECKING:
        # 组合后由DatabaseService提供的子库路由（前向声明，供类型检查）
        _room_manager: Callable[[int], Awaitable["DatabaseManager"]]

    @asynccontextmanager
    async def _event_session(self, room_id):
        """事件表会话上下文（路由到直播间子库）"""
        manager = await self._room_manager(room_id)
        async with manager.get_session() as session:
            yield session

    async def save_danmaku(
        self,
        room_id: int,
        uid: int,
        uname: str,
        msg: str,
        dm_type: int = 0,
        emoticon_url: Optional[str] = None,
        medal_name: Optional[str] = None,
        medal_level: int = 0
    ) -> None:
        """
        保存弹幕记录

        Args:
            room_id: 直播间ID
            uid: 用户ID
            uname: 用户名
            msg: 弹幕内容
            dm_type: 弹幕类型
            emoticon_url: 表情URL
            medal_name: 勋章名
            medal_level: 勋章等级
        """
        try:
            async with self._event_session(room_id) as session:
                record = DanmakuRecord(
                    room_id=room_id,
                    uid=uid,
                    uname=uname,
                    msg=msg,
                    dm_type=dm_type,
                    emoticon_url=emoticon_url,
                    medal_name=medal_name,
                    medal_level=medal_level,
                )
                session.add(record)
                await session.commit()
        except Exception as e:
            logger.error(f"保存弹幕记录失败: {e}")

    async def save_gift(
        self,
        room_id: int,
        uid: int,
        uname: str,
        gift_id: int,
        gift_name: str,
        num: int,
        price: int,
        coin_type: str
    ) -> None:
        """
        保存礼物记录

        Args:
            room_id: 直播间ID
            uid: 用户ID
            uname: 用户名
            gift_id: 礼物ID
            gift_name: 礼物名称
            num: 数量
            price: 单价
            coin_type: 瓜子类型
        """
        try:
            async with self._event_session(room_id) as session:
                record = GiftRecord(
                    room_id=room_id,
                    uid=uid,
                    uname=uname,
                    gift_id=gift_id,
                    gift_name=gift_name,
                    num=num,
                    price=price,
                    total_price=price * num,
                    coin_type=coin_type,
                )
                session.add(record)
                await session.commit()
        except Exception as e:
            logger.error(f"保存礼物记录失败: {e}")

    async def save_super_chat(
        self,
        room_id: int,
        uid: int,
        uname: str,
        price: int,
        message: str,
        background_color: Optional[str] = None
    ) -> None:
        """
        保存醒目留言记录

        Args:
            room_id: 直播间ID
            uid: 用户ID
            uname: 用户名
            price: 价格
            message: 留言内容
            background_color: 背景色
        """
        try:
            async with self._event_session(room_id) as session:
                record = SuperChatRecord(
                    room_id=room_id,
                    uid=uid,
                    uname=uname,
                    price=price,
                    message=message,
                    background_color=background_color,
                )
                session.add(record)
                await session.commit()
        except Exception as e:
            logger.error(f"保存SC记录失败: {e}")

    async def save_guard(
        self,
        room_id: int,
        uid: int,
        uname: str,
        guard_level: int,
        gift_name: str,
        price: int,
        num: int = 1
    ) -> None:
        """
        保存舰长记录

        Args:
            room_id: 直播间ID
            uid: 用户ID
            uname: 用户名
            guard_level: 舰长等级
            gift_name: 礼物名称
            price: 价格
            num: 数量
        """
        try:
            async with self._event_session(room_id) as session:
                record = GuardRecord(
                    room_id=room_id,
                    uid=uid,
                    uname=uname,
                    guard_level=guard_level,
                    gift_name=gift_name,
                    price=price,
                    num=num,
                )
                session.add(record)
                await session.commit()
        except Exception as e:
            logger.error(f"保存舰长记录失败: {e}")

    async def save_anchor(
        self,
        room_id: int,
        award_name: str,
        danmu: Optional[str] = None,
        require_text: Optional[str] = None,
        require_type: int = 0,
        require_value: str = ""
    ) -> None:
        """
        保存天选记录

        Args:
            room_id: 直播间ID
            award_name: 奖品名称
            danmu: 弹幕要求
            require_text: 要求文本
            require_type: 要求类型
            require_value: 要求值
        """
        try:
            async with self._event_session(room_id) as session:
                record = AnchorRecord(
                    room_id=room_id,
                    award_name=award_name,
                    danmu=danmu,
                    require_text=require_text,
                    require_type=require_type,
                    require_value=require_value,
                )
                session.add(record)
                await session.commit()
        except Exception as e:
            logger.error(f"保存天选记录失败: {e}")

    async def save_red_pocket(
        self,
        room_id: int,
        sender_uid: int,
        sender_name: str,
        total_price: int
    ) -> None:
        """
        保存红包记录

        Args:
            room_id: 直播间ID
            sender_uid: 发送者ID
            sender_name: 发送者名
            total_price: 总价值
        """
        try:
            async with self._event_session(room_id) as session:
                record = RedPocketRecord(
                    room_id=room_id,
                    sender_uid=sender_uid,
                    sender_name=sender_name,
                    total_price=total_price,
                )
                session.add(record)
                await session.commit()
        except Exception as e:
            logger.error(f"保存红包记录失败: {e}")

    async def save_guard_achievement(
        self,
        room_id: int,
        achieve_level: int,
        content: str
    ) -> None:
        """
        保存大航海成就记录

        Args:
            room_id: 直播间ID
            achieve_level: 成就等级
            content: 成就内容
        """
        try:
            async with self._event_session(room_id) as session:
                record = GuardAchievementRecord(
                    room_id=room_id,
                    achieve_level=achieve_level,
                    content=content,
                )
                session.add(record)
                await session.commit()
        except Exception as e:
            logger.error(f"保存大航海成就记录失败: {e}")

    async def save_system_event(
        self,
        room_id: int,
        event_type: EventType,
        uid: Optional[int] = None,
        uname: Optional[str] = None,
        msg: Optional[str] = None
    ) -> None:
        """
        保存系统事件记录

        Args:
            room_id: 直播间ID
            event_type: 事件类型
            uid: 相关用户ID
            uname: 相关用户名
            msg: 消息内容
        """
        try:
            async with self._event_session(room_id) as session:
                record = SystemEventRecord(
                    room_id=room_id,
                    event_type=event_type,
                    uid=uid,
                    uname=uname,
                    msg=msg,
                )
                session.add(record)
                await session.commit()
        except Exception as e:
            logger.error(f"保存系统事件记录失败: {e}")

    async def save_room_change(
        self,
        room_id: int,
        title: str,
        parent_area_id: int,
        parent_area_name: str,
        area_id: int,
        area_name: str
    ) -> None:
        """
        保存房间信息变更记录

        Args:
            room_id: 直播间ID
            title: 标题
            parent_area_id: 父分区ID
            parent_area_name: 父分区名
            area_id: 分区ID
            area_name: 分区名
        """
        try:
            async with self._event_session(room_id) as session:
                record = RoomChangeRecord(
                    room_id=room_id,
                    title=title,
                    parent_area_id=parent_area_id,
                    parent_area_name=parent_area_name,
                    area_id=area_id,
                    area_name=area_name,
                )
                session.add(record)
                await session.commit()
        except Exception as e:
            logger.error(f"保存房间变更记录失败: {e}")

    async def save_live_start(
        self,
        room_id: int,
        title: Optional[str] = None,
        cover: Optional[str] = None
    ) -> None:
        """
        保存开播记录

        Args:
            room_id: 直播间ID
            title: 标题
            cover: 封面
        """
        try:
            async with self._event_session(room_id) as session:
                record = LiveStatusRecord(
                    room_id=room_id,
                    status="live_start",
                    title=title,
                    cover=cover,
                    start_time=now_ms(),
                )
                session.add(record)
                await session.commit()
        except Exception as e:
            logger.error(f"保存开播记录失败: {e}")

    async def save_live_end(
        self,
        room_id: int,
        stats: "LiveStats",
        title: Optional[str] = None,
        cover: Optional[str] = None
    ) -> None:
        """
        保存下播记录

        Args:
            room_id: 直播间ID
            stats: 直播统计数据
            title: 标题
            cover: 封面
        """
        try:
            async with self._event_session(room_id) as session:
                record = LiveStatusRecord(
                    room_id=room_id,
                    status="live_end",
                    title=title,
                    cover=cover,
                    danmaku_count=stats.danmaku_count,
                    gold_count=stats.gold_count,
                    ship_count=stats.ship_count,
                    sc_count=stats.sc_count,
                    duration=stats.duration_seconds,
                    start_time=stats.start_time,
                    end_time=stats.end_time or now_ms(),
                )
                session.add(record)
                await session.commit()
        except Exception as e:
            logger.error(f"保存下播记录失败: {e}")

    # ========== 便捷方法 ==========

    async def save_cut_off(self, room_id: int, msg: str) -> None:
        """保存直播切断事件"""
        await self.save_system_event(room_id, EventType.CUT_OFF, msg=msg)

    async def save_warning(self, room_id: int, msg: str) -> None:
        """保存警告事件"""
        await self.save_system_event(room_id, EventType.WARNING, msg=msg)

    async def save_ban(self, room_id: int, uid: int, uname: str) -> None:
        """保存禁言事件"""
        await self.save_system_event(room_id, EventType.BAN, uid=uid, uname=uname)

    async def save_admin_set(self, room_id: int, uid: int) -> None:
        """保存设为房管事件"""
        await self.save_system_event(room_id, EventType.ADMIN_SET, uid=uid)

    async def save_admin_revoke(self, room_id: int, uid: int) -> None:
        """保存撤销房管事件"""
        await self.save_system_event(room_id, EventType.ADMIN_REVOKE, uid=uid)
