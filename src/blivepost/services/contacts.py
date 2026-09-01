# -*- coding: utf-8 -*-
"""
联系人存储与平台事件处理

ContactStore：好友/群联系状态（内存缓存+数据库），发送前据此过滤
未开启消息接收的目标；ContactEventHandler：处理botpy的联系人管理
事件（加/删好友、入群/退群、消息接收开关），退群时自动清理该群的
绑定与监控。
"""

from typing import Dict, List, Optional, TYPE_CHECKING

from ..utils.logger import get_logger


if TYPE_CHECKING:
    from ..database.service import DatabaseService
    from ..core.manager import MonitorManager
    from .bindings import BindingStore


logger = get_logger("contacts")


class ContactStore:
    """好友与群联系状态存储"""

    def __init__(self, db_service: "DatabaseService"):
        self._db = db_service
        # {openid: {"del": 0/1, "receive": 0/1, "name": str}}
        self._friends: Dict[str, dict] = {}
        self._groups: Dict[str, dict] = {}

    async def load(self) -> None:
        """从数据库加载好友与群记录"""
        friends = await self._db.load_friends()
        groups = await self._db.load_groups()
        self._friends = {
            f.openid: {"del": f.del_flag, "receive": f.msg_receive, "name": f.nickname}
            for f in friends
        }
        self._groups = {
            g.group_openid: {"del": g.del_flag, "receive": g.msg_receive, "name": g.group_name}
            for g in groups
        }
        logger.info(f"联系人已加载：好友{len(self._friends)}个，群{len(self._groups)}个")

    # ---- 群 ----

    def upsert_group(self, group_openid: str, name: str = "",
                     del_flag: int = 0, receive: int = 1) -> None:
        g = self._groups.setdefault(group_openid, {"del": 0, "receive": 1, "name": ""})
        g["del"] = del_flag
        g["receive"] = receive
        if name:
            g["name"] = name

    def set_group_del(self, group_openid: str, flag: int = 1) -> bool:
        g = self._groups.get(group_openid)
        if g is None:
            return False
        g["del"] = flag
        return True

    def set_group_receive(self, group_openid: str, receive: int) -> bool:
        g = self._groups.get(group_openid)
        if g is None:
            return False
        g["receive"] = receive
        return True

    def is_group_receiving(self, group_openid: str) -> bool:
        """群是否开启消息接收（未知群默认可发）"""
        g = self._groups.get(group_openid)
        return bool(g and g["receive"] == 1 and g["del"] == 0)

    def filter_receivable_groups(self, groups: List[str]) -> List[str]:
        """过滤出开启消息接收的群"""
        return [g for g in groups if self.is_group_receiving(g)]

    # ---- 好友 ----

    def upsert_friend(self, openid: str, name: str = "",
                      del_flag: int = 0, receive: int = 1) -> None:
        f = self._friends.setdefault(openid, {"del": 0, "receive": 1, "name": ""})
        f["del"] = del_flag
        f["receive"] = receive
        if name:
            f["name"] = name

    def set_friend_del(self, openid: str, flag: int = 1) -> bool:
        f = self._friends.get(openid)
        if f is None:
            return False
        f["del"] = flag
        return True

    def set_friend_receive(self, openid: str, receive: int) -> bool:
        f = self._friends.get(openid)
        if f is None:
            f = {"del": 0, "receive": receive, "name": ""}
            self._friends[openid] = f
            return True
        f["receive"] = receive
        return True

    def is_friend_receiving(self, openid: str) -> bool:
        f = self._friends.get(openid)
        return bool(f and f["receive"] == 1 and f["del"] == 0)

    # ---- 数据库同步 ----

    async def save_group(self, group_openid: str, name: str = "",
                         del_flag: int = 0, receive: int = 1) -> None:
        self.upsert_group(group_openid, name, del_flag, receive)
        await self._db.save_group(group_openid, name, del_flag, receive)

    async def save_friend(self, openid: str, name: str = "",
                          del_flag: int = 0, receive: int = 1) -> None:
        self.upsert_friend(openid, name, del_flag, receive)
        await self._db.save_friend(openid, name, del_flag, receive)

    async def set_group_receive_async(self, group_openid: str, receive: int) -> None:
        self.set_group_receive(group_openid, receive)
        await self._db.set_group_receive(group_openid, receive)

    async def set_friend_receive_async(self, openid: str, receive: int) -> None:
        self.set_friend_receive(openid, receive)
        await self._db.set_friend_receive(openid, receive)


class ContactEventHandler:
    """
    联系人平台事件处理器

    处理加/删好友、入群/退群、消息接收开关事件：
    退群时自动清理该群的全部绑定并停止监控（按需求）。
    """

    def __init__(
        self,
        contact_store: ContactStore,
        binding_store: "BindingStore",
        monitor_manager: "MonitorManager",
        db_service: Optional["DatabaseService"] = None,
    ):
        self.contact_store = contact_store
        self.binding_store = binding_store
        self.monitor_manager = monitor_manager
        self.db_service = db_service

    async def on_group_add(self, group_openid: str) -> None:
        await self.contact_store.save_group(group_openid, del_flag=0, receive=1)
        logger.info(f"机器人加入群 {group_openid}")

    async def on_group_del(self, group_openid: str) -> None:
        """被移出群：标记移除 + 清理该群全部绑定与监控"""
        await self.contact_store.save_group(group_openid, del_flag=1, receive=0)
        logger.warning(f"机器人被移出群 {group_openid}，清理绑定与监控")

        # 清理该群全部房间绑定并停止监控
        rooms = self._rooms_bound_to(group_openid)
        for rid in rooms:
            await self.binding_store.unbind_all_for_group(group_openid, rid)
            if self.monitor_manager is not None:
                await self.monitor_manager.remove(rid)

    def _rooms_bound_to(self, group_openid: str) -> List[int]:
        return [b.room_id for b in self.binding_store.get_group_bindings(group_openid)]

    async def on_group_receive(self, group_openid: str, receive: int) -> None:
        await self.contact_store.set_group_receive_async(group_openid, receive)

    async def on_friend_add(self, openid: str) -> None:
        await self.contact_store.save_friend(openid, del_flag=0, receive=1)
        logger.info(f"新增好友 {openid}")

    async def on_friend_del(self, openid: str) -> None:
        await self.contact_store.save_friend(openid, del_flag=1, receive=0)
        logger.info(f"好友 {openid} 已删除")
