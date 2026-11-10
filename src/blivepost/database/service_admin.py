# -*- coding: utf-8 -*-
"""
数据库存储服务 - 管理功能（AdminMixin）
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .models import (
    DanmakuRecord, GiftRecord, SuperChatRecord, GuardRecord,
    AnchorRecord, RedPocketRecord, GuardAchievementRecord,
    SystemEventRecord, RoomChangeRecord, LiveStatusRecord, local_now,
    GroupBindingRecord, CookieAccountRecord, DynamicBindingRecord,
    FriendRecord, GroupRecord, StreamerRecord, LiveRoomRecord,
    WebUserRecord, PushSwitchRecord, GroupPushSettingRecord, GroupAtAllRecord
)
from ..utils.logger import get_logger

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager
    from .manager import DatabaseManager
    from .room_store import RoomDbRegistry

# 群独立推送设置覆盖的开关（与GroupPushSettingRecord列对应；动态监控为全局项不在其中）
GROUP_PUSH_SWITCHES = ("anchor", "guard_achievement", "system_event", "room_change",
                       "daily_report", "weekly_report", "monthly_report")


logger = get_logger("db_service")


class AdminMixin:
    """管理功能混入（绑定/账号池/订阅/好友与群/主播与直播间信息、Web面板用户与推送开关、导出与旧数据迁移；组合进DatabaseService）"""

    if TYPE_CHECKING:
        # 组合后由DatabaseService或其他混入提供的能力（前向声明，供类型检查）
        db_manager: "DatabaseManager"
        _event_session: Callable[[Any], "AbstractAsyncContextManager[Any]"]
        _room_manager: Callable[[int], Awaitable["DatabaseManager"]]
        _ensure_registry: Callable[[], "RoomDbRegistry"]

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
                        monitored=1 if monitored else 0,
                    )
                    session.add(record)
                else:
                    record.uid = uid
                    record.monitored = 1 if monitored else 0
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
            async with self._event_session(room_id) as session:
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

    # ========== 群信息快照（群基本信息接口） ==========

    async def update_group_info(self, group_openid: str, info: dict) -> None:
        """
        写入群基本信息快照（get_group_info返回结构）

        任意查询数据项为空时填入NULL（不写空串）；group_tags为
        字符串数组，序列化为JSON文本存储。
        """
        def _clean(value) -> Optional[str]:
            if value is None:
                return None
            text = str(value).strip()
            return text or None

        tags_raw = info.get("group_tags")
        tag_list = ([str(t).strip() for t in tags_raw if str(t).strip()]
                    if isinstance(tags_raw, list) else [])
        member_raw = info.get("group_member_num")
        try:
            member_count = int(member_raw) if member_raw is not None else None
        except (TypeError, ValueError):
            member_count = None

        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(GroupRecord).where(
                        GroupRecord.group_openid == group_openid))
                record = result.scalar_one_or_none()
                if record is None:
                    session.add(GroupRecord(
                        group_openid=group_openid,
                        group_name=_clean(info.get("group_name")) or "",
                        group_desc=_clean(info.get("group_finger_memo")),
                        category=_clean(info.get("group_class_text")),
                        tags=(json.dumps(tag_list, ensure_ascii=False)
                              if tag_list else None),
                        member_count=member_count,
                    ))
                else:
                    name_clean = _clean(info.get("group_name"))
                    if name_clean:
                        record.group_name = name_clean
                    record.group_desc = _clean(info.get("group_finger_memo"))
                    record.category = _clean(info.get("group_class_text"))
                    record.tags = (json.dumps(tag_list, ensure_ascii=False)
                                   if tag_list else None)
                    record.member_count = member_count
                await session.commit()
        except Exception as e:
            logger.error(f"更新群信息失败({group_openid}): {e}")

    async def list_group_openids(self) -> List[str]:
        """bot_group表全部群openid"""
        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(select(GroupRecord.group_openid))
                return [str(r[0]) for r in res.all()]
        except Exception as e:
            logger.error(f"查询群openid列表失败: {e}")
            return []

    async def list_groups_missing_info(self) -> List[str]:
        """群信息存在空值（任一新字段为NULL）的群openid列表"""
        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(
                    select(GroupRecord.group_openid).where(
                        GroupRecord.group_desc.is_(None)
                        | GroupRecord.category.is_(None)
                        | GroupRecord.tags.is_(None)
                        | GroupRecord.member_count.is_(None)
                    ))
                return [str(r[0]) for r in res.all()]
        except Exception as e:
            logger.error(f"查询缺失群信息的群失败: {e}")
            return []

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

    async def list_rooms(self) -> list:
        """全部已添加直播间（直播间注册表，Web面板直播监控页用）"""
        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(
                    select(LiveRoomRecord).order_by(LiveRoomRecord.room_id))
                return list(res.scalars().all())
        except Exception as e:
            logger.error(f"查询直播间列表失败: {e}")
            return []

    async def get_room_uid(self, room_id: int) -> int:
        """查询直播间对应主播UID（注册表，无记录返回0）"""
        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(
                    select(LiveRoomRecord.uid).where(
                        LiveRoomRecord.room_id == room_id))
                return int(res.scalar_one_or_none() or 0)
        except Exception as e:
            logger.error(f"查询直播间主播UID失败(room={room_id}): {e}")
            return 0

    async def delete_live_room(self, room_id: int) -> bool:
        """从直播间注册表移除（子库数据保留）"""
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(LiveRoomRecord).where(
                        LiveRoomRecord.room_id == room_id))
                record = result.scalar_one_or_none()
                if record is None:
                    return False
                await session.delete(record)
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"移除直播间注册失败(room={room_id}): {e}")
            return False

    async def list_bindings(self) -> list:
        """全部群推送目标关系（Web面板绑定状态表用）"""
        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(select(GroupBindingRecord))
                return list(res.scalars().all())
        except Exception as e:
            logger.error(f"查询推送关系失败: {e}")
            return []

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

    @staticmethod
    def get_db_file_size_mb(db_manager: "DatabaseManager") -> Optional[float]:
        """获取SQLite数据库文件体积MB（非sqlite返回None）"""
        path = getattr(db_manager, "db_path", None)
        if path and os.path.exists(path):
            return round(os.path.getsize(path) / (1024 * 1024), 2)
        return None

    async def get_storage_size_mb(self) -> float:
        """全部存储体积合计MB（主库+全部房间子库+动态子库目录）"""
        total = 0.0
        main_path = getattr(self.db_manager, "db_path", None)
        if main_path and os.path.exists(main_path):
            total += os.path.getsize(main_path)
        registry = self._ensure_registry()
        files = set(registry.existing_db_files())
        for d in (registry.base_dir.parent / "dynamics", Path("data") / "dynamics"):
            if d.is_dir():
                files.update(d.rglob("*.db"))
        total += sum(f.stat().st_size for f in files if f.exists())
        return round(total / (1024 * 1024), 2)

    async def migrate_legacy_events(self) -> int:
        """
        旧库事件数据迁移：主库中遗留的事件表数据按房间搬入子库（启动时调用）

        搬移=复制到房间子库+删除主库对应行（数据不丢失，仅更换存放位置）；
        幂等：主库事件表清空后自动跳过。

        Returns:
            int: 搬移总行数
        """
        moved_total = 0
        legacy_models: Tuple[Any, ...] = (
            DanmakuRecord, GiftRecord, SuperChatRecord, GuardRecord,
            AnchorRecord, RedPocketRecord, GuardAchievementRecord,
            SystemEventRecord, RoomChangeRecord, LiveStatusRecord)
        for model in legacy_models:
            try:
                moved_total += await self._migrate_legacy_table(model)
            except Exception as e:
                logger.warning(f"迁移{model.__tablename__}跳过: {e}")
        if moved_total:
            logger.info(f"旧库事件数据迁移完成：共{moved_total}行转入房间子库")
        return moved_total

    async def _migrate_legacy_table(self, model) -> int:
        """
        单张事件表的分块搬移（查500行→写子库→删主库，循环至清空）

        幂等：复制与删除仍是跨库的两个事务，中途崩溃重启后重跑安全——
        复制阶段用 INSERT ... ON CONFLICT DO NOTHING 跳过子库已存在的 id
        （上次崩溃已复制未删除的行），删除阶段按本批已落到子库的 id 列表
        删除（无论本次新插入还是上次已存在），不会重复搬移/主键冲突卡死。
        """
        # 新装库主库本就没有事件表（子库架构后create_all不再建入主库），静默跳过
        if not await self.db_manager.has_table(model.__tablename__):
            return 0
        moved = 0
        table = model.__table__
        cols = [c.name for c in table.columns]
        while True:
            async with self.db_manager.get_session() as session:
                res = await session.execute(select(model).limit(500))
                rows = res.scalars().all()
            if not rows:
                break
            ids = [r.id for r in rows]
            by_room: Dict[int, list] = {}
            for r in rows:
                data = {c: getattr(r, c) for c in cols}
                by_room.setdefault(r.room_id, []).append(data)
            for room_id, items in by_room.items():
                manager = await self._room_manager(room_id)
                async with manager.get_session() as session:
                    # 子库为sqlite：id已存在（上次崩溃已复制）则整行跳过
                    await session.execute(
                        sqlite_insert(model).values(items).on_conflict_do_nothing())
                    await session.commit()
            async with self.db_manager.get_session() as session:
                await session.execute(table.delete().where(table.c.id.in_(ids)))
                await session.commit()
            moved += len(rows)
            logger.info(f"迁移{table.name}: 已搬移{moved}行")
        return moved

    # ========== Web面板：前台用户与全局推送开关 ==========

    def sqlite_path(self) -> str:
        """主库文件路径（用于后台数据导出；由DatabaseConfig提供）"""
        return self.db_manager.config.sqlite_path

    async def create_web_user(self, username: str, password_hash: str) -> bool:
        """新建前台用户（用户名唯一）；已存在返回False"""
        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(select(WebUserRecord).where(
                    WebUserRecord.username == username))
                if res.scalar_one_or_none() is not None:
                    return False
                session.add(WebUserRecord(username=username,
                                          password_hash=password_hash))
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"创建Web用户失败: {e}")
            return False

    async def get_web_user(self, username: str) -> Optional[dict]:
        """按用户名查询前台用户（None=不存在）"""
        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(select(WebUserRecord).where(
                    WebUserRecord.username == username))
                record = res.scalar_one_or_none()
                if record is None:
                    return None
                return {"username": record.username,
                        "password_hash": record.password_hash,
                        "created_at": record.created_at}
        except Exception as e:
            logger.error(f"查询Web用户失败: {e}")
            return None

    async def list_web_users(self) -> List[dict]:
        """全部前台用户（按创建时间升序）"""
        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(
                    select(WebUserRecord).order_by(WebUserRecord.created_at))
                return [{"username": r.username, "created_at": r.created_at}
                        for r in res.scalars().all()]
        except Exception as e:
            logger.error(f"列出Web用户失败: {e}")
            return []

    async def update_web_user_password(self, username: str,
                                       password_hash: str) -> bool:
        """重置前台用户口令（用户不存在返回False）"""
        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(select(WebUserRecord).where(
                    WebUserRecord.username == username))
                record = res.scalar_one_or_none()
                if record is None:
                    return False
                record.password_hash = password_hash
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"重置Web用户口令失败: {e}")
            return False

    async def delete_web_user(self, username: str) -> bool:
        """删除前台用户"""
        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(select(WebUserRecord).where(
                    WebUserRecord.username == username))
                record = res.scalar_one_or_none()
                if record is None:
                    return False
                await session.delete(record)
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"删除Web用户失败: {e}")
            return False

    async def seed_push_switches(self, defaults: Dict[str, bool]) -> None:
        """播种全局推送开关（仅插入缺失行；已有行不覆盖——后台修改优先）"""
        try:
            async with self.db_manager.get_session() as session:
                for name, enabled in defaults.items():
                    res = await session.execute(select(PushSwitchRecord).where(
                        PushSwitchRecord.name == name))
                    if res.scalar_one_or_none() is None:
                        session.add(PushSwitchRecord(name=name,
                                                     enabled=1 if enabled else 0,
                                                     allow_modify=0))
                await session.commit()
        except Exception as e:
            logger.error(f"播种推送开关失败: {e}")

    async def get_push_switches(self) -> Dict[str, Dict[str, bool]]:
        """全部推送开关 {name: {enabled, allow_modify}}（缺行时不含）"""
        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(select(PushSwitchRecord))
                return {r.name: {"enabled": bool(r.enabled),
                                 "allow_modify": bool(r.allow_modify)}
                        for r in res.scalars().all()}
        except Exception as e:
            logger.error(f"查询推送开关失败: {e}")
            return {}

    async def get_push_switch_enabled(self, name: str,
                                      default: bool = False) -> bool:
        """单个推送开关状态（行缺失回退default）"""
        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(select(PushSwitchRecord.enabled).where(
                    PushSwitchRecord.name == name))
                row = res.first()
                return bool(row[0]) if row is not None else default
        except Exception as e:
            logger.error(f"查询推送开关失败: {e}")
            return default

    async def acquire_at_all_groups(self, groups: List[str]) -> List[str]:
        """艾特全体配额：返回本次可艾特的群（每群每自然日1次）。

        当天未使用的群在调用时即登记为已用（调度即占用，发送失败不退还；
        平台配额按实际送达计，此策略保守但简单，避免重试重复艾特）。
        """
        groups = [g for g in dict.fromkeys(groups) if g]
        if not groups:
            return []
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(select(GroupAtAllRecord).where(
                    GroupAtAllRecord.group_openid.in_(groups)))
                used = {r.group_openid: r.used_date for r in res.scalars().all()}
                eligible = [g for g in groups if used.get(g) != today]
                for g in eligible:
                    record = await session.get(GroupAtAllRecord, g)
                    if record is None:
                        session.add(GroupAtAllRecord(group_openid=g, used_date=today))
                    else:
                        record.used_date = today
                await session.commit()
                return eligible
        except Exception as e:
            logger.error(f"艾特全体配额判定失败: {e}")
            return []

    async def clear_group_at_all(self, group_openid: str) -> bool:
        """删除群的艾特全体配额记录（退群清理；重进群后重新计数）"""
        try:
            async with self.db_manager.get_session() as session:
                result = await session.execute(
                    select(GroupAtAllRecord).where(
                        GroupAtAllRecord.group_openid == group_openid))
                record = result.scalar_one_or_none()
                if record is None:
                    return False
                await session.delete(record)
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"清理艾特全体配额记录失败({group_openid}): {e}")
            return False

    async def set_push_switch(self, name: str, enabled: Optional[bool] = None,
                              allow_modify: Optional[bool] = None) -> bool:
        """更新推送开关（行缺失则按给定值或默认关创建）；有变更返回True"""
        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(select(PushSwitchRecord).where(
                    PushSwitchRecord.name == name))
                record = res.scalar_one_or_none()
                if record is None:
                    record = PushSwitchRecord(
                        name=name,
                        enabled=1 if enabled else 0,
                        allow_modify=1 if allow_modify else 0)
                    session.add(record)
                else:
                    if enabled is not None:
                        record.enabled = 1 if enabled else 0
                    if allow_modify is not None:
                        record.allow_modify = 1 if allow_modify else 0
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"更新推送开关失败: {e}")
            return False

    # ========== 群独立推送设置（房间子库；行缺省回落全局开关） ==========

    async def get_group_push_settings(self, room_id) -> Dict[str, Dict[str, bool]]:
        """指定房间的全部群推送设置 {group: {switch: bool}}（房间子库）"""
        try:
            async with self._event_session(int(room_id)) as session:
                res = await session.execute(select(GroupPushSettingRecord))
                return {r.group_openid: {s: bool(getattr(r, s))
                                         for s in GROUP_PUSH_SWITCHES}
                        for r in res.scalars().all()}
        except Exception as e:
            logger.error(f"查询群推送设置失败(房间{room_id}): {e}")
            return {}

    async def get_group_push_setting(self, room_id,
                                     group_openid: str) -> Optional[Dict[str, bool]]:
        """单个群的独立推送设置（无行返回None=跟随全局）"""
        try:
            async with self._event_session(int(room_id)) as session:
                res = await session.execute(select(GroupPushSettingRecord).where(
                    GroupPushSettingRecord.group_openid == group_openid))
                r = res.scalar_one_or_none()
                if r is None:
                    return None
                return {s: bool(getattr(r, s)) for s in GROUP_PUSH_SWITCHES}
        except Exception as e:
            logger.error(f"查询群推送设置失败(房间{room_id} 群{group_openid}): {e}")
            return None

    async def set_group_push_setting(self, room_id, group_openid: str,
                                     switch: str, enabled: bool) -> bool:
        """翻转群独立开关（行缺省时创建；switch须属GROUP_PUSH_SWITCHES）"""
        if switch not in GROUP_PUSH_SWITCHES:
            return False
        try:
            async with self._event_session(int(room_id)) as session:
                res = await session.execute(select(GroupPushSettingRecord).where(
                    GroupPushSettingRecord.group_openid == group_openid))
                r = res.scalar_one_or_none()
                if r is None:
                    r = GroupPushSettingRecord(group_openid=group_openid)
                    session.add(r)
                setattr(r, switch, 1 if enabled else 0)
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"更新群推送设置失败(房间{room_id} 群{group_openid}): {e}")
            return False

    async def reset_group_push_setting(self, room_id, group_openid: str) -> bool:
        """删除群独立设置行（恢复跟随全局）"""
        try:
            async with self._event_session(int(room_id)) as session:
                res = await session.execute(select(GroupPushSettingRecord).where(
                    GroupPushSettingRecord.group_openid == group_openid))
                r = res.scalar_one_or_none()
                if r is None:
                    return False
                await session.delete(r)
                await session.commit()
                return True
        except Exception as e:
            logger.error(f"重置群推送设置失败(房间{room_id} 群{group_openid}): {e}")
            return False

    async def seed_group_push_settings(self, room_id, groups, defaults: Dict[str, bool]) -> int:
        """为房间缺行的绑定群播种独立设置（继承全局开关值）；返回播种行数（幂等）"""
        if not groups:
            return 0
        try:
            async with self._event_session(int(room_id)) as session:
                res = await session.execute(select(GroupPushSettingRecord.group_openid))
                existing = {r[0] for r in res.all()}
                added = 0
                for g in groups:
                    if g in existing:
                        continue
                    session.add(GroupPushSettingRecord(
                        group_openid=g,
                        **{s: 1 if defaults.get(s) else 0 for s in GROUP_PUSH_SWITCHES}))
                    added += 1
                if added:
                    await session.commit()
                    logger.info(f"房间{room_id}播种群推送设置{added}行（继承全局）")
                return added
        except Exception as e:
            logger.error(f"播种群推送设置失败(房间{room_id}): {e}")
            return 0
