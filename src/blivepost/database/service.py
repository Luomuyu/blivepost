# -*- coding: utf-8 -*-
"""
数据库存储服务
"""

import os
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from sqlalchemy import func, select

from .manager import DatabaseManager
from .models import (
    DanmakuRecord, GiftRecord, SuperChatRecord, GuardRecord,
    AnchorRecord, RedPocketRecord, GuardAchievementRecord,
    SystemEventRecord, RoomChangeRecord, LiveStatusRecord, EventType, local_now,
    GroupBindingRecord, CookieAccountRecord, DynamicBindingRecord,
    FriendRecord, GroupRecord, StreamerRecord, LiveRoomRecord
)
from ..utils.logger import get_logger


if TYPE_CHECKING:
    from ..models.live import LiveStats


logger = get_logger("db_service")


class DatabaseService:
    """数据库存储服务"""

    def __init__(self, db_manager: DatabaseManager):
        """
        初始化数据库服务

        Args:
            db_manager: 数据库管理器
        """
        self.db_manager = db_manager

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
            async with self.db_manager.get_session() as session:
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
            async with self.db_manager.get_session() as session:
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
            async with self.db_manager.get_session() as session:
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
            async with self.db_manager.get_session() as session:
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
            async with self.db_manager.get_session() as session:
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
            async with self.db_manager.get_session() as session:
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
            async with self.db_manager.get_session() as session:
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
            async with self.db_manager.get_session() as session:
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
            async with self.db_manager.get_session() as session:
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
            async with self.db_manager.get_session() as session:
                record = LiveStatusRecord(
                    room_id=room_id,
                    status="live_start",
                    title=title,
                    cover=cover,
                    start_time=local_now(),
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
            async with self.db_manager.get_session() as session:
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
                    end_time=stats.end_time or local_now(),
                )
                session.add(record)
                await session.commit()
        except Exception as e:
            logger.error(f"保存下播记录失败: {e}")

    # ========== 绑定记录 ==========

    async def save_binding(
        self,
        group_openid: str,
        room_id: int,
        uid: int = 0,
        monitored: bool = False
    ) -> None:
        """
        保存/更新群绑定记录（同群同房间仅一条）

        Args:
            group_openid: 群openid
            room_id: 直播间ID
            uid: 主播UID
            monitored: 是否处于监控状态
        """
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(GroupBindingRecord).where(
                        GroupBindingRecord.group_openid == group_openid,
                        GroupBindingRecord.room_id == room_id,
                    )
                )
                record = result.scalar_one_or_none()
                if record is None:
                    record = GroupBindingRecord(
                        group_openid=group_openid,
                        room_id=room_id,
                        uid=uid,
                        monitored=monitored,
                    )
                    session.add(record)
                else:
                    record.uid = uid
                    record.monitored = monitored
                await session.commit()
        except Exception as e:
            logger.error(f"保存绑定记录失败: {e}")

    async def delete_binding(self, group_openid: str, room_id: int) -> bool:
        """
        删除群绑定记录

        Args:
            group_openid: 群openid
            room_id: 直播间ID

        Returns:
            bool: 是否删除了记录
        """
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(GroupBindingRecord).where(
                        GroupBindingRecord.group_openid == group_openid,
                        GroupBindingRecord.room_id == room_id,
                    )
                )
                record = result.scalar_one_or_none()
                if record is None:
                    return False
                await session.delete(record)
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"删除绑定记录失败: {e}")
            return False

    async def set_binding_monitored(
        self,
        group_openid: str,
        room_id: int,
        monitored: bool
    ) -> bool:
        """
        更新绑定记录的监控状态

        Args:
            group_openid: 群openid
            room_id: 直播间ID
            monitored: 监控状态

        Returns:
            bool: 记录是否存在并被更新
        """
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(GroupBindingRecord).where(
                        GroupBindingRecord.group_openid == group_openid,
                        GroupBindingRecord.room_id == room_id,
                    )
                )
                record = result.scalar_one_or_none()
                if record is None:
                    return False
                record.monitored = monitored
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"更新绑定监控状态失败: {e}")
            return False

    async def load_bindings(self) -> list:
        """
        加载全部绑定记录

        Returns:
            list[GroupBindingRecord]: 绑定记录列表（失败时返回空列表）
        """
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(select(GroupBindingRecord))
                return list(result.scalars().all())
        except Exception as e:
            logger.error(f"加载绑定记录失败: {e}")
            return []

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

    # ========== 统计查询 ==========

    async def get_daily_summary(self, room_id, day: Optional[datetime] = None,
                                _window=None) -> dict:
        """
        查询指定房间某日（默认今天）的收入与弹幕汇总

        各表聚合以标量子查询组合为单条SQL执行，避免跨表串行往返。

        Args:
            room_id: 直播间ID
            day: 日期（取该日0点到24点），默认今天

        Returns:
            dict: {
                day, danmaku_count, gift_gold(元), gift_count,
                ship_total(元), ship_count, sc_total(元), sc_count, red_pocket(电池)
            }
        """
        if _window is not None:
            day_start, day_end = _window
        else:
            day = day or local_now()
            day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)

        summary = {
            "day": day_start.strftime("%Y-%m-%d"),
            "danmaku_count": 0,
            "gift_gold": 0.0,
            "gift_count": 0,
            "ship_total": 0.0,
            "ship_count": 0,
            "sc_total": 0,
            "sc_count": 0,
            "red_pocket": 0,
        }

        def _scalar(model, aggregate, *filters):
            """单列标量子查询（每表每个聚合一个，组合进同一条SQL）"""
            room_filter = (model.room_id.in_(room_id) if isinstance(room_id, (list, tuple))
                           else model.room_id == room_id)
            return select(aggregate).select_from(model).where(
                room_filter,
                model.timestamp >= day_start,
                model.timestamp < day_end,
                *filters,
            ).scalar_subquery()

        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(
                    select(
                        _scalar(DanmakuRecord, func.count()).label("danmaku_count"),
                        # 礼物（金瓜子，瓜子→元）
                        _scalar(GiftRecord, func.coalesce(func.sum(GiftRecord.total_price), 0),
                                GiftRecord.coin_type == "gold").label("gift_gold"),
                        _scalar(GiftRecord, func.count(),
                                GiftRecord.coin_type == "gold").label("gift_count"),
                        # 上舰（瓜子→元）
                        _scalar(GuardRecord, func.coalesce(
                            func.sum(GuardRecord.price * GuardRecord.num), 0)
                        ).label("ship_gold"),
                        _scalar(GuardRecord, func.count()).label("ship_count"),
                        # SC（元）
                        _scalar(SuperChatRecord, func.coalesce(
                            func.sum(SuperChatRecord.price), 0)).label("sc_total"),
                        _scalar(SuperChatRecord, func.count()).label("sc_count"),
                        # 红包（电池）
                        _scalar(RedPocketRecord, func.coalesce(
                            func.sum(RedPocketRecord.total_price), 0)).label("red_pocket"),
                    )
                )
                row = res.one()

                summary["danmaku_count"] = row.danmaku_count or 0
                summary["gift_gold"] = round(int(row.gift_gold) / 1000, 1)
                summary["gift_count"] = row.gift_count
                summary["ship_total"] = round(int(row.ship_gold) / 1000, 1)
                summary["ship_count"] = row.ship_count
                summary["sc_total"] = int(row.sc_total)
                summary["sc_count"] = row.sc_count
                summary["red_pocket"] = int(row.red_pocket or 0)
        except Exception as e:
            logger.error(f"查询每日汇总失败: {e}")

        return summary

    async def get_summary_range(self, room_id: int, start: datetime, end: datetime) -> dict:
        """
        查询指定房间时间段 [start, end) 的收入与弹幕汇总（周报/月报用）

        Args:
            room_id: 直播间ID
            start: 起始时间（含）
            end: 结束时间（不含）

        Returns:
            dict: 结构同get_daily_summary，day字段为时间范围文本
        """
        summary = await self.get_daily_summary(room_id, _window=(start, end))
        summary["day"] = f"{start.strftime('%Y-%m-%d')}~{end.strftime('%Y-%m-%d')}"
        return summary

    async def get_top_super_chat(
        self,
        room_id: int,
        day: Optional[datetime] = None,
        limit: int = 5
    ) -> List[Tuple[str, int, str]]:
        """
        查询指定房间某日（默认今天）SC金额排行

        Args:
            room_id: 直播间ID
            day: 日期，默认今天
            limit: 条数

        Returns:
            list[(uname, price, message)]，按金额降序
        """
        day = day or local_now()
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(
                    select(SuperChatRecord.uname, SuperChatRecord.price,
                           SuperChatRecord.message)
                    .where(
                        SuperChatRecord.room_id == room_id,
                        SuperChatRecord.timestamp >= day_start,
                        SuperChatRecord.timestamp < day_end,
                    )
                    .order_by(SuperChatRecord.price.desc())
                    .limit(limit)
                )
                return [(r.uname, r.price, r.message) for r in res.all()]
        except Exception as e:
            logger.error(f"查询SC排行失败: {e}")
            return []

    # ========== Cookie账号池 ==========

    async def save_cookie_account(
        self,
        uid: int,
        nickname: str,
        cookie_string: str,
        sessdata: str = "",
        bili_jct: str = "",
        ac_time_value: str = "",
        sessdata_expires_at: Optional[datetime] = None,
        status: str = "active"
    ) -> None:
        """
        保存/更新账号cookie（按uid upsert）

        Args:
            uid: B站用户UID
            nickname: 昵称
            cookie_string: 完整cookie字符串
            sessdata: SESSDATA
            bili_jct: csrf
            ac_time_value: refresh token
            sessdata_expires_at: SESSDATA过期时间
            status: 状态
        """
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(CookieAccountRecord).where(CookieAccountRecord.uid == uid)
                )
                record = result.scalar_one_or_none()
                if record is None:
                    record = CookieAccountRecord(uid=uid)
                    session.add(record)
                record.nickname = nickname
                record.cookie_string = cookie_string
                record.sessdata = sessdata
                record.bili_jct = bili_jct
                record.ac_time_value = ac_time_value
                record.sessdata_expires_at = sessdata_expires_at
                record.status = status
                record.last_error = None
                await session.commit()
        except Exception as e:
            logger.error(f"保存账号cookie失败: {e}")

    async def get_cookie_accounts(self) -> list:
        """获取全部账号cookie记录"""
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(select(CookieAccountRecord))
                return list(result.scalars().all())
        except Exception as e:
            logger.error(f"查询账号cookie失败: {e}")
            return []

    async def set_cookie_account_status(
        self,
        uid: int,
        status: str,
        error: Optional[str] = None
    ) -> bool:
        """
        更新账号状态（active/expired）

        Args:
            uid: B站用户UID
            status: 新状态
            error: 失效原因

        Returns:
            bool: 账号是否存在
        """
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(CookieAccountRecord).where(CookieAccountRecord.uid == uid)
                )
                record = result.scalar_one_or_none()
                if record is None:
                    return False
                record.status = status
                record.last_error = error
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"更新账号状态失败: {e}")
            return False

    async def touch_cookie_account(self, uid: int) -> None:
        """更新账号最近使用时间（轮转调度用）"""
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(CookieAccountRecord).where(CookieAccountRecord.uid == uid)
                )
                record = result.scalar_one_or_none()
                if record is not None:
                    record.last_used_at = local_now()
                    record.use_count = (record.use_count or 0) + 1
                    await session.commit()
        except Exception as e:
            logger.error(f"更新账号使用时间失败: {e}")

    async def delete_cookie_account(self, uid: int) -> bool:
        """
        删除账号cookie记录

        Args:
            uid: B站用户UID

        Returns:
            bool: 是否删除了记录
        """
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(CookieAccountRecord).where(CookieAccountRecord.uid == uid)
                )
                record = result.scalar_one_or_none()
                if record is None:
                    return False
                await session.delete(record)
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"删除账号cookie失败: {e}")
            return False

    async def export_day_records(
        self,
        room_id: int,
        day: Optional[datetime] = None
    ) -> dict:
        """
        导出指定房间某日（默认今天）的全部事件明细

        Args:
            room_id: 直播间ID
            day: 日期，默认今天

        Returns:
            dict: {表名: [行元组列表]}，含 danmaku/gift/super_chat/guard/red_pocket
        """
        day = day or local_now()
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        async def _fetch(model, columns):
            async with self.db_manager.get_session() as session:
                res = await session.execute(
                    select(*[getattr(model, c) for c in columns]).where(
                        model.room_id == room_id,
                        model.timestamp >= day_start,
                        model.timestamp < day_end,
                    ).order_by(model.timestamp)
                )
                return [tuple(r) for r in res.all()]

        result = {}
        for name, model, cols in [
            ("danmaku", DanmakuRecord, ["timestamp", "uid", "uname", "msg", "dm_type"]),
            ("gift", GiftRecord,
             ["timestamp", "uid", "uname", "gift_name", "num", "total_price", "coin_type"]),
            ("super_chat", SuperChatRecord, ["timestamp", "uid", "uname", "price", "message"]),
            ("guard", GuardRecord, ["timestamp", "uid", "uname", "guard_level", "gift_name", "price", "num"]),
            ("red_pocket", RedPocketRecord, ["timestamp", "sender_uid", "sender_name", "total_price"]),
        ]:
            try:
                result[name] = await _fetch(model, cols)
            except Exception as e:
                logger.error(f"导出{name}明细失败: {e}")
                result[name] = []
        return result

    # ========== 动态订阅 ==========

    async def save_dynamic_binding(self, group_openid: str, up_uid: int, up_name: str = "") -> None:
        """保存/更新动态订阅（同群同UP仅一条）"""
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(DynamicBindingRecord).where(
                        DynamicBindingRecord.group_openid == group_openid,
                        DynamicBindingRecord.up_uid == up_uid,
                    )
                )
                record = result.scalar_one_or_none()
                if record is None:
                    session.add(DynamicBindingRecord(
                        group_openid=group_openid, up_uid=up_uid, up_name=up_name))
                else:
                    if up_name:
                        record.up_name = up_name
                await session.commit()
        except Exception as e:
            logger.error(f"保存动态订阅失败: {e}")

    async def delete_dynamic_binding(self, group_openid: str, up_uid: int) -> bool:
        """删除动态订阅"""
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(DynamicBindingRecord).where(
                        DynamicBindingRecord.group_openid == group_openid,
                        DynamicBindingRecord.up_uid == up_uid,
                    )
                )
                record = result.scalar_one_or_none()
                if record is None:
                    return False
                await session.delete(record)
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"删除动态订阅失败: {e}")
            return False

    async def load_dynamic_bindings(self) -> list:
        """加载全部动态订阅"""
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(select(DynamicBindingRecord))
                return list(result.scalars().all())
        except Exception as e:
            logger.error(f"加载动态订阅失败: {e}")
            return []

    async def update_dynamic_up_name(self, up_uid: int, up_name: str) -> None:
        """批量更新UP主昵称（首次轮询到后回填）"""
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(DynamicBindingRecord).where(
                        DynamicBindingRecord.up_uid == up_uid)
                )
                for record in result.scalars().all():
                    record.up_name = up_name
                await session.commit()
        except Exception as e:
            logger.error(f"更新UP主昵称失败: {e}")

    async def get_recent_live_status(self, limit: int = 10) -> list:
        """
        查询最近的下播记录（Web面板用）

        Args:
            limit: 条数

        Returns:
            list[dict]: 下播记录列表
        """
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(LiveStatusRecord).where(
                        LiveStatusRecord.status == "live_end"
                    ).order_by(LiveStatusRecord.timestamp.desc()).limit(limit)
                )
                return [
                    {
                        "room_id": rec.room_id,
                        "title": rec.title,
                        "danmaku_count": rec.danmaku_count,
                        "gold_count": rec.gold_count,
                        "ship_count": rec.ship_count,
                        "sc_count": rec.sc_count,
                        "duration": rec.duration,
                        "end_time": rec.end_time,
                    }
                    for rec in result.scalars().all()
                ]
        except Exception as e:
            logger.error(f"查询下播记录失败: {e}")
            return []

    async def get_daily_history(self, room_ids: Optional[List[int]] = None,
                                days: int = 30) -> List[dict]:
        """
        按日聚合的历史趋势数据（Web面板折线图用）

        Args:
            room_ids: 房间列表（None=全部房间）
            days: 统计最近N天

        Returns:
            list[dict]: [{"day": "YYYY-MM-DD", "danmaku": n, "gift_gold": f,
                          "ship_gold": f, "sc_total": n}] 按日期升序
        """
        today = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
        return await self.get_daily_history_range(
            (today - timedelta(days=days - 1)).date(), today.date(),
            room_ids=room_ids)

    async def get_daily_history_range(self, start_day, end_day,
                                      room_ids: Optional[List[int]] = None) -> List[dict]:
        """
        按日聚合的自然日区间历史数据（Web面板趋势选择器用）

        Args:
            start_day/end_day: 起止日期（date/datetime，含首尾）
            room_ids: 房间列表（None=全部房间）

        Returns:
            list[dict]: 同get_daily_history，按日期升序；区间内无数据补零
        """
        from datetime import datetime as _dt, date as _date, timedelta as _td
        if isinstance(start_day, _date) and not isinstance(start_day, _dt):
            start_day = _dt.combine(start_day, _dt.min.time())
        if isinstance(end_day, _date) and not isinstance(end_day, _dt):
            end_day = _dt.combine(end_day, _dt.min.time())
        start = start_day
        span_days = (end_day - start_day).days + 1
        if span_days <= 0:
            return []
        end_bound = start + _td(days=span_days)

        async def _by_day(model, aggregate, coin_filter=None):
            conditions = [model.timestamp >= start, model.timestamp < end_bound]
            if coin_filter is not None:
                conditions.append(coin_filter)
            if room_ids is not None:
                conditions.append(model.room_id.in_(room_ids))
            async with self.db_manager.get_session() as session:
                res = await session.execute(
                    select(
                        func.date(model.timestamp).label("day"),
                        aggregate,
                    ).select_from(model).where(*conditions).group_by("day")
                )
                return {str(r.day): int(r[1] or 0) for r in res.all()}

        history: Dict[str, dict] = {}
        try:
            danmaku = await _by_day(DanmakuRecord, func.count())
            gift = await _by_day(GiftRecord, func.sum(GiftRecord.total_price),
                                 GiftRecord.coin_type == "gold")
            ship = await _by_day(GuardRecord, func.sum(GuardRecord.price * GuardRecord.num))
            sc = await _by_day(SuperChatRecord, func.sum(SuperChatRecord.price))
        except Exception as e:
            logger.error(f"查询历史趋势失败: {e}")
            return []

        for i in range(span_days):
            day = (start + _td(days=i)).strftime("%Y-%m-%d")
            history[day] = {
                "day": day,
                "danmaku": danmaku.get(day, 0),
                "gift_gold": round(gift.get(day, 0) / 1000, 1),
                "ship_gold": round(ship.get(day, 0) / 1000, 1),
                "sc_total": sc.get(day, 0),
            }
        return [history[day] for day in sorted(history)]

    # ========== 好友与群记录 ==========

    async def save_friend(self, openid: str, name: str = "",
                          del_flag: int = 0, receive: int = 1,
                          union_openid: Optional[str] = None) -> None:
        """保存/更新好友记录（按openid upsert；union_openid仅在有值时覆盖）"""
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(FriendRecord).where(FriendRecord.openid == openid))
                record = result.scalar_one_or_none()
                if record is None:
                    session.add(FriendRecord(openid=openid, nickname=name,
                                             del_flag=del_flag, msg_receive=receive,
                                             union_openid=union_openid))
                else:
                    if name:
                        record.nickname = name
                    record.del_flag = del_flag
                    record.msg_receive = receive
                    if union_openid:
                        record.union_openid = union_openid
                await session.commit()
        except Exception as e:
            logger.error(f"保存好友记录失败: {e}")

    async def load_friends(self) -> list:
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(select(FriendRecord))
                return list(result.scalars().all())
        except Exception as e:
            logger.error(f"加载好友记录失败: {e}")
            return []

    async def set_friend_receive(self, openid: str, receive: int) -> None:
        """更新好友消息接收开关（不存在则创建）"""
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(FriendRecord).where(FriendRecord.openid == openid))
                record = result.scalar_one_or_none()
                if record is None:
                    session.add(FriendRecord(openid=openid, msg_receive=receive))
                else:
                    record.msg_receive = receive
                await session.commit()
        except Exception as e:
            logger.error(f"更新好友接收开关失败: {e}")

    async def update_friend_union(self, openid: str, union_openid: str) -> None:
        """补充好友的union_openid（消息中获取到时回填）"""
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(FriendRecord).where(FriendRecord.openid == openid))
                record = result.scalar_one_or_none()
                if record is not None and union_openid:
                    record.union_openid = union_openid
                    await session.commit()
        except Exception as e:
            logger.error(f"回填好友union_openid失败: {e}")

    async def save_group(self, group_openid: str, name: str = "",
                         del_flag: int = 0, receive: int = 1) -> None:
        """保存/更新群记录（按openid upsert）"""
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(GroupRecord).where(GroupRecord.group_openid == group_openid))
                record = result.scalar_one_or_none()
                if record is None:
                    session.add(GroupRecord(group_openid=group_openid, group_name=name,
                                            del_flag=del_flag, msg_receive=receive))
                else:
                    if name:
                        record.group_name = name
                    record.del_flag = del_flag
                    record.msg_receive = receive
                await session.commit()
        except Exception as e:
            logger.error(f"保存群记录失败: {e}")

    # ========== 主播与直播间信息快照 ==========

    async def save_streamer_info(self, uid: int, uname: str = "", face: str = "",
                                 room_id: int = 0) -> None:
        """保存/更新主播信息（按uid upsert；字段仅有值时覆盖）"""
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(StreamerRecord).where(StreamerRecord.uid == uid))
                record = result.scalar_one_or_none()
                if record is None:
                    session.add(StreamerRecord(uid=uid, uname=uname, face=face,
                                               room_id=room_id))
                else:
                    if uname:
                        record.uname = uname
                    if face:
                        record.face = face
                    if room_id:
                        record.room_id = room_id
                await session.commit()
        except Exception as e:
            logger.error(f"保存主播信息失败(uid={uid}): {e}")

    async def save_live_room_info(self, room_id: int, uid: int = 0, title: str = "",
                                  short_id: int = 0, live_status: Optional[int] = None,
                                  area_name: str = "", parent_area_name: str = "",
                                  cover: str = "", keyframe: str = "",
                                  online: Optional[int] = None) -> None:
        """保存/更新直播间信息（按room_id upsert；字段仅有值时覆盖）"""
        if not room_id:
            return
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(LiveRoomRecord).where(LiveRoomRecord.room_id == room_id))
                record = result.scalar_one_or_none()
                if record is None:
                    session.add(LiveRoomRecord(
                        room_id=room_id, uid=uid, title=title, short_id=short_id,
                        live_status=live_status if live_status is not None else 0,
                        area_name=area_name, parent_area_name=parent_area_name,
                        cover=cover, keyframe=keyframe,
                        online=online if online is not None else 0))
                else:
                    if uid:
                        record.uid = uid
                    if title:
                        record.title = title
                    if short_id:
                        record.short_id = short_id
                    if live_status is not None:
                        record.live_status = live_status
                    if area_name:
                        record.area_name = area_name
                    if parent_area_name:
                        record.parent_area_name = parent_area_name
                    if cover:
                        record.cover = cover
                    if keyframe:
                        record.keyframe = keyframe
                    if online is not None:
                        record.online = online
                await session.commit()
        except Exception as e:
            logger.error(f"保存直播间信息失败(room={room_id}): {e}")

    async def apply_status_info(self, status_by_uid: dict) -> None:
        """
        批量写入主播/直播间信息快照（get_status_info_by_uids返回结构，键为主播uid）

        每个uid的条目同时更新streamer（uname/face/room_id）与live_room
        （title/short_id/live_status/分区/封面/在线）两表。
        """
        for uid_key, info in status_by_uid.items():
            try:
                uid = int(uid_key)
                room_id = int(info.get("room_id") or 0)
                await self.save_streamer_info(
                    uid, uname=str(info.get("uname") or ""),
                    face=str(info.get("face") or ""), room_id=room_id)
                await self.save_live_room_info(
                    room_id, uid=uid, title=str(info.get("title") or ""),
                    short_id=int(info.get("short_id") or 0),
                    live_status=int(info.get("live_status") or 0),
                    area_name=str(info.get("area_v2_name") or info.get("area_name") or ""),
                    parent_area_name=str(info.get("area_v2_parent_name") or ""),
                    cover=str(info.get("cover_from_user") or ""),
                    keyframe=str(info.get("keyframe") or ""),
                    online=int(info.get("online") or 0))
            except (ValueError, TypeError) as e:
                logger.warning(f"跳过无效的状态条目({uid_key}): {e}")

    async def get_streamer_name(self, uid: int) -> str:
        """查询主播昵称（streamer表，绑定时写入/每日0点刷新）"""
        if not uid:
            return ""
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(StreamerRecord.uname).where(StreamerRecord.uid == uid))
                return str(result.scalar_one_or_none() or "")
        except Exception as e:
            logger.error(f"查询主播昵称失败(uid={uid}): {e}")
            return ""

    async def streamer_count(self) -> int:
        """主播表行数（启动回填判断用）"""
        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(select(func.count()).select_from(StreamerRecord))
                return int(res.scalar() or 0)
        except Exception as e:
            logger.error(f"查询主播表行数失败: {e}")
            return 0

    async def load_groups(self) -> list:
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(select(GroupRecord))
                return list(result.scalars().all())
        except Exception as e:
            logger.error(f"加载群记录失败: {e}")
            return []

    async def set_group_receive(self, group_openid: str, receive: int) -> None:
        """更新群消息接收开关（不存在则创建）"""
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(GroupRecord).where(GroupRecord.group_openid == group_openid))
                record = result.scalar_one_or_none()
                if record is None:
                    session.add(GroupRecord(group_openid=group_openid, msg_receive=receive))
                else:
                    record.msg_receive = receive
                await session.commit()
        except Exception as e:
            logger.error(f"更新群接收开关失败: {e}")

    async def get_table_counts(self) -> List[Tuple[str, int]]:
        """
        统计各数据表行数（数据治理用）

        Returns:
            list[(表名, 行数)]，按行数降序
        """
        tables = [
            ("弹幕", DanmakuRecord), ("礼物", GiftRecord),
            ("SC", SuperChatRecord), ("舰长", GuardRecord),
            ("天选", AnchorRecord), ("红包", RedPocketRecord),
            ("大航海成就", GuardAchievementRecord), ("系统事件", SystemEventRecord),
            ("房间变更", RoomChangeRecord), ("开播下播", LiveStatusRecord),
            ("群绑定", GroupBindingRecord), ("Cookie账号", CookieAccountRecord),
            ("动态订阅", DynamicBindingRecord),
        ]
        result: List[Tuple[str, int]] = []
        try:
            async with self.db_manager.get_session() as session:
                for name, model in tables:
                    res = await session.execute(select(func.count()).select_from(model))
                    result.append((name, int(res.scalar() or 0)))
        except Exception as e:
            logger.error(f"统计表行数失败: {e}")
        return sorted(result, key=lambda x: x[1], reverse=True)

    @staticmethod
    def get_db_file_size_mb(db_manager: "DatabaseManager") -> Optional[float]:
        """获取SQLite数据库文件体积MB（非sqlite返回None）"""
        path = getattr(db_manager, "db_path", None)
        if path and os.path.exists(path):
            return round(os.path.getsize(path) / (1024 * 1024), 2)
        return None
