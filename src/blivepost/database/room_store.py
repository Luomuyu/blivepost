# -*- coding: utf-8 -*-
"""
直播间子数据库管理（sqlite专用，不受config数据库类型控制）

每个直播间的事件数据（弹幕/礼物/舰长/SC/天选/红包/大航海成就/
系统事件/房间变更/开播下播）存放在独立sqlite文件：
    <数据目录>/rooms/room_{room_id}.db
主库（config选择的库）仅经room_db表记录直播间→子库文件名的映射。
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .models import EventBase, set_db_type
from ..utils.logger import get_logger


logger = get_logger("room_store")


class RoomDbManager:
    """单个直播间子数据库（sqlite）"""

    def __init__(self, path: Path):
        self.db_path = str(path)
        self._engine: Any = None
        self._session_factory: Optional[async_sessionmaker] = None

    async def initialize(self) -> None:
        """建引擎与表结构（幂等）"""
        if self._engine is not None:
            return
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        set_db_type("sqlite")
        engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}")
        async with engine.begin() as conn:
            await conn.run_sync(EventBase.metadata.create_all)
        self._engine = engine
        self._session_factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False)
        logger.debug(f"直播间子库已就绪: {self.db_path}")

    def get_session(self) -> AsyncSession:
        """获取数据库会话"""
        assert self._session_factory is not None, "子库尚未初始化"
        return self._session_factory()

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None


class RoomDbRegistry:
    """直播间子数据库注册表（按文件名缓存管理器）"""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self._managers: Dict[str, RoomDbManager] = {}

    async def get(self, db_name: str) -> RoomDbManager:
        """取（或创建并初始化）指定子库"""
        manager = self._managers.get(db_name)
        if manager is None:
            manager = RoomDbManager(self.base_dir / db_name)
            await manager.initialize()
            self._managers[db_name] = manager
        return manager

    async def all(self) -> List[RoomDbManager]:
        """全部已加载的子库管理器"""
        return list(self._managers.values())

    def existing_db_files(self) -> List[Path]:
        """目录下实际存在的子库文件（含本次运行未加载的）"""
        if not self.base_dir.exists():
            return []
        return sorted(self.base_dir.glob("*.db"))
