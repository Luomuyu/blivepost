# -*- coding: utf-8 -*-
"""
直播监控模块
"""

import http.cookies
from typing import Any, Optional, TYPE_CHECKING

import aiohttp

from ..config.settings import Settings
from ..models.live import LiveRoomInfo, LiveStats, TempData
from ..services.push import PushService
from ..services.bilibili import BilibiliAPI
from ..services.fingerprint import FingerprintService
from ..utils.logger import get_logger
from .handler import create_handler_with_base
from ..vendor.patch import apply_patch

if TYPE_CHECKING:
    from ..database import DatabaseService


logger = get_logger("monitor")


class LiveMonitor:
    """直播监控器"""

    def __init__(self, config_path: Optional[str] = None, sessdata: str = ""):
        """
        初始化直播监控器

        Args:
            config_path: 配置文件路径
            sessdata: B站登录凭证
        """
        # 加载配置
        self.settings = Settings.load(config_path)

        # 保存sessdata
        self._sessdata = sessdata

        # 初始化指纹服务
        self.fingerprint = FingerprintService(self.settings, sessdata)

        # 初始化服务
        self.push_service = PushService(self.settings)
        self.bilibili_api: Any = BilibiliAPI(sessdata)

        # 设置API的fp_cookie
        fp_cookie = self.fingerprint.get_cookie()
        if fp_cookie:
            self.bilibili_api.set_fp_cookie(fp_cookie)

        # 设置push_service的bilibili_api引用
        self.push_service.bilibili_api = self.bilibili_api

        # 数据库服务（由外部设置）
        self.db_service: Optional["DatabaseService"] = None

        # 状态数据
        self.stats = LiveStats()
        self.temp_data = TempData()
        self.room_info: Optional[LiveRoomInfo] = None

        # 事件处理器（延迟创建）
        self.handler = None

        # 客户端和session
        self._client: Any = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False

    def set_sessdata(self, sessdata: str) -> None:
        """
        设置B站登录凭证（同步更新到指纹服务与API服务）

        Args:
            sessdata: B站登录凭证
        """
        self._sessdata = sessdata
        self.bilibili_api.sessdata = sessdata
        self.fingerprint.sessdata = sessdata

    def _init_session(self) -> aiohttp.ClientSession:
        """
        初始化带登录凭证的HTTP Session

        Returns:
            aiohttp.ClientSession: 配置好的session
        """
        cookies = http.cookies.SimpleCookie()
        if self._sessdata:
            cookies['SESSDATA'] = self._sessdata
            cookies['SESSDATA']['domain'] = 'bilibili.com'

        session = aiohttp.ClientSession()
        session.cookie_jar.update_cookies(cookies)
        return session

    async def start(self) -> None:
        """启动监控"""
        if self._running:
            logger.warning("监控已在运行中")
            return

        room_id = self.settings.live.room_id
        if not room_id:
            logger.error("未配置直播间ID，请检查配置文件")
            raise ValueError("直播间ID未配置")

        logger.info(f"正在启动直播监控，房间号：{room_id}")

        try:
            # 获取直播间信息
            self.room_info = self.bilibili_api.get_room_info(room_id)
            logger.info(
                f"直播间信息 - 主播：{self.room_info.name}，"
                f"标题：{self.room_info.title}，"
                f"房间号：{self.room_info.room_id}"
            )

            # 导入blivedm
            from ..vendor.importer import import_blivedm
            blivedm = import_blivedm()

            # 应用blivedm补丁
            apply_patch(self.settings.message.ignore_cmds)

            # 初始化带sessdata的session
            self._session = self._init_session()

            # 创建客户端（携带登录凭证）
            self._client = blivedm.BLiveClient(
                int(room_id),
                session=self._session,
            )

            # 创建事件处理器（动态继承BaseHandler）
            self.handler = create_handler_with_base(
                self.settings,
                self.push_service,
                self.stats,
                self.temp_data,
                room_id,
                self.db_service
            )

            # 注册事件处理器
            self._client.set_handler(self.handler)

            # 启动客户端（同步方法）
            self._client.start()
            self._running = True
            logger.info("监控已启动，等待直播间消息...")

            # 等待客户端运行
            await self._client.join()

        except Exception as e:
            logger.error(f"启动监控失败：{e}")
            # 清理session
            if self._session:
                await self._session.close()
                self._session = None
            raise

    async def stop(self) -> None:
        """停止监控"""
        if not self._running:
            return

        logger.info("正在停止监控...")
        self._running = False

        if self._client:
            try:
                await self._client.stop_and_close()
            except Exception as e:
                logger.warning(f"停止客户端时出错: {e}")
            self._client = None

        # 关闭session
        if self._session:
            try:
                await self._session.close()
            except Exception as e:
                logger.warning(f"关闭session时出错: {e}")
            self._session = None

        logger.info("监控已停止")

    @property
    def is_running(self) -> bool:
        """是否正在运行"""
        return self._running

    def get_status(self) -> dict:
        """
        获取监控状态

        Returns:
            dict: 状态信息
        """
        return {
            "running": self._running,
            "room_id": self.settings.live.room_id,
            "room_info": {
                "name": self.room_info.name if self.room_info else None,
                "title": self.room_info.title if self.room_info else None,
                "online": self.room_info.online if self.room_info else None,
            } if self.room_info else None,
            "is_live": self.handler.is_live if self.handler else False,
            "stats": {
                "danmaku_count": self.stats.danmaku_count,
                "gold_count": self.stats.gold_count,
                "ship_count": self.stats.ship_count,
                "sc_count": self.stats.sc_count,
                "like_count": self.stats.like_count,
                "duration": self.stats.duration_str if self.stats.start_time else "0",
            }
        }
