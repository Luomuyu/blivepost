# -*- coding: utf-8 -*-
"""
数据库存储服务
"""

from pathlib import Path
from typing import Optional

from sqlalchemy import select

from .manager import DatabaseManager
from .models import RoomDbRecord
from .room_store import RoomDbRegistry
from .service_admin import GROUP_PUSH_SWITCHES, AdminMixin
from .service_analytics import AnalyticsMixin
from .service_events import EventWriterMixin
from .service_stats import StatsMixin
from ..utils.logger import get_logger

# 兼容旧导入路径：GROUP_PUSH_SWITCHES原定义于本模块（现移至service_admin，此处re-export）
__all__ = ["DatabaseService", "GROUP_PUSH_SWITCHES"]


logger = get_logger("db_service")


class DatabaseService(EventWriterMixin, StatsMixin, AdminMixin, AnalyticsMixin):
    """数据库存储服务"""

    def __init__(self, db_manager: DatabaseManager):
        """
        初始化数据库服务

        Args:
            db_manager: 数据库管理器
        """
        self.db_manager = db_manager
        # 直播间事件子库注册表（惰性：依赖主库初始化后的db_path）
        self._registry: Optional[RoomDbRegistry] = None
        # 房间→子库文件名进程内缓存（写穿透：随登记写入；映射行仅本类写入，无失效源）
        # 热路径（每条弹幕/礼物落库）原本每事件开一次主库会话查映射，缓存后零主库查询
        self._room_db_cache: dict = {}

    # ========== 直播间子库路由 ==========

    def _ensure_registry(self) -> RoomDbRegistry:
        if self._registry is None:
            db_path = getattr(self.db_manager, "db_path", None)
            if db_path:
                # 按主库文件名隔离子库目录（同目录多主库互不串库）
                base = Path(db_path).parent / "rooms" / Path(db_path).stem
            else:
                # MySQL/PG主库无文件路径：锚定配置文件目录而非进程CWD
                from ..config.settings import config_dir
                base = config_dir() / "data" / "rooms"
            self._registry = RoomDbRegistry(base)
        return self._registry

    async def aclose(self) -> None:
        """释放全部房间子库连接（主库engine由DatabaseManager.close负责）"""
        if self._registry is not None:
            await self._registry.close_all()
            self._registry = None

    async def _room_db_name(self, room_id: int) -> str:
        """主库room_db映射：查子库文件名，无则登记默认名（带进程内缓存）"""
        default = f"room_{int(room_id)}.db"
        cached = self._room_db_cache.get(int(room_id))
        if cached:
            return cached
        name: Optional[str] = None
        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(
                    select(RoomDbRecord.db_name).where(RoomDbRecord.room_id == room_id))
                name = res.scalar_one_or_none()
                if not name:
                    session.add(RoomDbRecord(room_id=int(room_id), db_name=default))
                    await session.commit()
                    name = default
        except Exception as e:
            # 失败不缓存：保留下次重试登记的机会，行为与无缓存时一致
            logger.warning(f"登记房间子库映射失败(room={room_id}): {e}")
            return default
        self._room_db_cache[int(room_id)] = name
        return name

    async def _room_manager(self, room_id: int):
        """事件数据目标子库（查映射→加载/创建子库）"""
        name = await self._room_db_name(room_id)
        return await self._ensure_registry().get(name)

    async def _all_room_managers(self) -> list:
        """全部房间子库（已加载的 + 主库映射登记过的）"""
        registry = self._ensure_registry()
        names = set()
        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(select(RoomDbRecord.db_name))
                names = {row[0] for row in res.all()}
        except Exception as e:
            logger.warning(f"读取房间子库映射失败: {e}")
        return [await registry.get(n) for n in sorted(names)]
