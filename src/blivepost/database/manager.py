# -*- coding: utf-8 -*-
"""
数据库连接管理
"""

from pathlib import Path
from typing import Optional

from sqlalchemy.ext.asyncio import (
    AsyncSession, AsyncEngine, create_async_engine, async_sessionmaker
)
from sqlalchemy import text

from ..config.settings import DatabaseConfig
from ..utils.logger import get_logger
from .migrations import run_migrations
from .models import Base, set_db_type


logger = get_logger("database")


class DatabaseManager:
    """数据库连接管理器"""
    
    def __init__(self, config: DatabaseConfig):
        """
        初始化数据库管理器
        
        Args:
            config: 数据库配置
        """
        self.config = config
        self._engine: Optional[AsyncEngine] = None
        self._session_factory: Optional[async_sessionmaker[AsyncSession]] = None
    
    def _get_database_url(self) -> str:
        """
        根据配置生成数据库连接URL
        
        Returns:
            数据库连接URL
        """
        db_type = self.config.type.lower()
        
        if db_type == "sqlite":
            # 确保SQLite数据库目录存在
            db_path = Path(self.config.sqlite_path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            return f"sqlite+aiosqlite:///{db_path}"
        
        elif db_type == "mysql":
            return (
                f"mysql+aiomysql://{self.config.user}:{self.config.password}"
                f"@{self.config.host}:{self.config.port}/{self.config.database}"
                f"?charset=utf8mb4"
            )
        
        elif db_type == "postgresql":
            return (
                f"postgresql+asyncpg://{self.config.user}:{self.config.password}"
                f"@{self.config.host}:{self.config.port}/{self.config.database}"
            )
        
        else:
            raise ValueError(f"不支持的数据库类型: {db_type}")
    
    async def initialize(self) -> None:
        """初始化数据库连接和表结构"""
        if self._engine is not None:
            return
        
        db_url = self._get_database_url()
        db_type = self.config.type.lower()
        
        # 设置数据库类型，用于动态选择主键类型
        set_db_type(db_type)
        
        logger.info(f"正在连接数据库: {db_type}")
        
        # 创建异步引擎
        if db_type == "sqlite":
            self._engine = create_async_engine(
                db_url,
                echo=False,
                future=True,
            )
        else:
            self._engine = create_async_engine(
                db_url,
                echo=False,
                future=True,
                pool_size=5,
                max_overflow=10,
                pool_pre_ping=True,
            )
        
        # 创建会话工厂
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        
        # 创建缺失表（新表）
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # 增量Schema迁移（旧库补列/变更，跳过已应用版本）
        await run_migrations(self._engine)
        
        logger.info("数据库初始化完成")
    
    async def close(self) -> None:
        """关闭数据库连接"""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            logger.info("数据库连接已关闭")
    
    def get_session(self) -> AsyncSession:
        """
        获取数据库会话
        
        Returns:
            异步数据库会话
        """
        if self._session_factory is None:
            raise RuntimeError("数据库未初始化，请先调用 initialize()")
        return self._session_factory()
    
    @property
    def engine(self) -> AsyncEngine:
        """获取数据库引擎"""
        if self._engine is None:
            raise RuntimeError("数据库未初始化，请先调用 initialize()")
        return self._engine
    
    async def test_connection(self) -> bool:
        """
        测试数据库连接
        
        Returns:
            连接是否成功
        """
        try:
            async with self.get_session() as session:
                await session.execute(text("SELECT 1"))
            logger.info("数据库连接测试成功")
            return True
        except Exception as e:
            logger.error(f"数据库连接测试失败: {e}")
            return False
