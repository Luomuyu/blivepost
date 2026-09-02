# -*- coding: utf-8 -*-
"""
群绑定存储服务

维护群openid与直播间的绑定关系（含监控状态），内存缓存供推送
路由同步查询，变更异步持久化到数据库。
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional

from ..database.models import GroupBindingRecord
from ..utils.logger import get_logger


if TYPE_CHECKING:
    from ..database.service import DatabaseService


logger = get_logger("bindings")


@dataclass
class Binding:
    """绑定关系"""
    group_openid: str
    room_id: int
    uid: int = 0
    monitored: bool = False


class BindingStore:
    """群绑定存储"""

    def __init__(self, db_service: "DatabaseService"):
        """
        初始化绑定存储

        Args:
            db_service: 数据库存储服务
        """
        self._db = db_service
        # {group_openid: {room_id: Binding}}
        self._bindings: Dict[str, Dict[int, Binding]] = {}
        # 动态订阅：{up_uid: {group_openid: up_name}}
        self._dynamics: Dict[int, Dict[str, str]] = {}

    async def load(self) -> int:
        """
        从数据库加载全部绑定到内存

        Returns:
            int: 加载的绑定数量
        """
        records: List[GroupBindingRecord] = await self._db.load_bindings()
        self._bindings.clear()
        # 动态订阅一并加载
        dyn_records = await self._db.load_dynamic_bindings()
        self._dynamics = {}
        for d in dyn_records:
            self._dynamics.setdefault(d.up_uid, {})[d.group_openid] = d.up_name
        for rec in records:
            binding = Binding(
                group_openid=rec.group_openid,
                room_id=rec.room_id,
                uid=rec.uid,
                monitored=rec.monitored,
            )
            self._bindings.setdefault(binding.group_openid, {})[binding.room_id] = binding
        logger.info(f"已加载 {len(records)} 条群绑定记录，{len(dyn_records)} 条动态订阅")
        return len(records)

    async def bind(self, group_openid: str, room_id: int, uid: int = 0) -> Binding:
        """
        绑定群与直播间（已存在则更新uid）

        Args:
            group_openid: 群openid
            room_id: 直播间ID
            uid: 主播UID

        Returns:
            Binding: 绑定关系
        """
        binding = self.get_binding(group_openid, room_id)
        if binding is None:
            binding = Binding(group_openid=group_openid, room_id=room_id, uid=uid)
            self._bindings.setdefault(group_openid, {})[room_id] = binding
        else:
            binding.uid = uid

        await self._db.save_binding(group_openid, room_id, uid, binding.monitored)
        return binding

    async def unbind(self, group_openid: str, room_id: int) -> bool:
        """
        解除群与直播间的绑定

        Args:
            group_openid: 群openid
            room_id: 直播间ID

        Returns:
            bool: 是否存在绑定并被解除
        """
        room_bindings = self._bindings.get(group_openid)
        if not room_bindings or room_id not in room_bindings:
            return False

        del room_bindings[room_id]
        if not room_bindings:
            del self._bindings[group_openid]

        await self._db.delete_binding(group_openid, room_id)
        return True

    async def set_monitored(self, group_openid: str, room_id: int, monitored: bool) -> bool:
        """
        设置绑定的监控状态

        Args:
            group_openid: 群openid
            room_id: 直播间ID
            monitored: 监控状态

        Returns:
            bool: 绑定是否存在并被更新
        """
        binding = self.get_binding(group_openid, room_id)
        if binding is None:
            return False

        binding.monitored = monitored
        await self._db.set_binding_monitored(group_openid, room_id, monitored)
        return True

    def get_binding(self, group_openid: str, room_id: int) -> Optional[Binding]:
        """查询绑定关系（同步）"""
        return self._bindings.get(group_openid, {}).get(room_id)

    def get_monitored_groups(self, room_id: int) -> List[str]:
        """
        获取指定直播间处于监控状态的群openid列表（同步，推送路由用）

        Args:
            room_id: 直播间ID

        Returns:
            List[str]: 群openid列表
        """
        return [
            b.group_openid
            for bindings in self._bindings.values()
            for b in bindings.values()
            if b.room_id == room_id and b.monitored
        ]

    def get_monitored_room_ids(self) -> List[int]:
        """
        获取全部处于监控状态的直播间ID（同步，启动恢复用）

        Returns:
            List[int]: 去重后的直播间ID列表
        """
        return sorted({
            b.room_id
            for bindings in self._bindings.values()
            for b in bindings.values()
            if b.monitored
        })

    def get_bound_uids(self) -> List[int]:
        """
        获取全部绑定关系中的主播uid（去重、去0；每日0点批量刷新用）

        Returns:
            List[int]: 排序后的主播uid列表
        """
        return sorted({
            b.uid
            for bindings in self._bindings.values()
            for b in bindings.values()
            if b.uid
        })

    def get_group_bindings(self, group_openid: str) -> List[Binding]:
        """获取群的所有绑定关系"""
        return list(self._bindings.get(group_openid, {}).values())

    # ========== 动态订阅 ==========

    async def bind_dynamic(self, group_openid: str, up_uid: int, up_name: str = "") -> bool:
        """
        绑定群与UP主动态

        Returns:
            bool: 是否为新增绑定（False表示已存在）
        """
        groups = self._dynamics.setdefault(up_uid, {})
        is_new = group_openid not in groups
        groups[group_openid] = up_name or groups.get(group_openid, "")
        await self._db.save_dynamic_binding(group_openid, up_uid, up_name)
        return is_new

    async def unbind_dynamic(self, group_openid: str, up_uid: int) -> bool:
        """
        解除群与UP主的动态订阅

        Returns:
            bool: 是否存在订阅并被解除
        """
        groups = self._dynamics.get(up_uid)
        if not groups or group_openid not in groups:
            return False
        del groups[group_openid]
        if not groups:
            del self._dynamics[up_uid]
        await self._db.delete_dynamic_binding(group_openid, up_uid)
        return True

    def get_dynamic_groups(self, up_uid: int) -> List[str]:
        """获取订阅了指定UP主的群openid列表（推送路由用）"""
        return list(self._dynamics.get(up_uid, {}).keys())

    def get_dynamic_up_uids(self) -> List[int]:
        """全部被订阅的UP主UID（启动恢复用）"""
        return sorted(self._dynamics.keys())

    def get_dynamic_up_name(self, up_uid: int) -> str:
        """获取UP主昵称（任意订阅记录中的名称）"""
        groups = self._dynamics.get(up_uid, {})
        for name in groups.values():
            if name:
                return name
        return ""

    def get_group_dynamic_ups(self, group_openid: str) -> List[int]:
        """查询群订阅的全部UP主UID"""
        return sorted(uid for uid, groups in self._dynamics.items()
                      if group_openid in groups)

    async def update_dynamic_up_name(self, up_uid: int, up_name: str) -> None:
        """回填UP主昵称（内存+数据库）"""
        groups = self._dynamics.get(up_uid)
        if groups:
            for gid in groups:
                groups[gid] = up_name
        await self._db.update_dynamic_up_name(up_uid, up_name)
