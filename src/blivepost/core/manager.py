# -*- coding: utf-8 -*-
"""
多房间监控管理

主进程不监控直播间：每个房间由独立的子进程连接弹幕服务器并处理事件，
子进程产生的推送与落库请求经跨进程队列发回主进程统一处理
（推送按绑定关系路由，落库集中写入避免SQLite多进程写冲突）。

- RoomProcess：单个房间的子进程封装，进程死亡由watcher即时重建
- MonitorManager：管理全部房间子进程，保证每个roomid只有一个监控进程
- QueueDbBridge/QueuePushService/_room_process_main：子进程侧（见room_process.py）
- PushQueueConsumer：主进程侧的队列消费者（见consumer.py）
- CookieMaintainer/StreamerRefresher/GroupInfoRefresher：维护循环（见maintenance.py）

本模块re-export各子模块的公共名字，保持blivepost.core.manager导入路径不变。
"""

import asyncio
import multiprocessing
import time

from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config.settings import Settings
from ..models.live import LiveRoomInfo
from ..services.bilibili import BilibiliAPI
from ..utils.logger import get_logger
from .consumer import PushQueueConsumer
from .maintenance import CookieMaintainer, GroupInfoRefresher, StreamerRefresher
from .room_process import (
    RECONCILE_INTERVAL,
    QueueDbBridge,
    QueuePushService,
    _room_process_main,
)


__all__ = [
    "MAX_CONSECUTIVE_CRASHES",
    "MAX_REBUILD_DELAY",
    "REBUILD_DELAY",
    "RECONCILE_INTERVAL",
    "STABLE_UPTIME",
    "CookieMaintainer",
    "GroupInfoRefresher",
    "MonitorManager",
    "PushQueueConsumer",
    "QueueDbBridge",
    "QueuePushService",
    "RoomProcess",
    "StreamerRefresher",
]


logger = get_logger("manager")

# 子进程异常退出后的重建基础延迟（秒），连续快速崩溃按指数退避
REBUILD_DELAY = 5
# 重建退避上限（秒）：5s→10s→20s→40s→80s→160s后封顶
MAX_REBUILD_DELAY = 300
# 连续快速崩溃的最大重建次数，超过后放弃自动重建（重新ensure可恢复）
MAX_CONSECUTIVE_CRASHES = 6
# 子进程稳定运行时长（秒）：超过则崩溃计数重新开始
STABLE_UPTIME = 600


class RoomProcess:
    """单房间监控子进程封装"""

    def __init__(
        self,
        room_id: int,
        settings: Settings,
        queue,
        ctx,
        log_dir: Optional[str] = None,
        on_exit: Optional[Callable[[int], Any]] = None,
        cookie_uid: int = 0,
        cookie_string: str = ""
    ):
        """
        初始化房间进程

        Args:
            room_id: 直播间ID
            settings: 应用配置
            queue: 跨进程队列
            ctx: multiprocessing上下文（spawn）
            log_dir: 日志目录（子进程共用）
            on_exit: 进程意外退出时的回调（主进程重建用）
            cookie_uid: 使用的账号池账号UID（0表示未用账号池）
            cookie_string: 该账号的完整cookie字符串
        """
        self.room_id = room_id
        self.cookie_uid = cookie_uid
        self.settings = settings
        self._queue = queue
        self._ctx = ctx
        self._log_dir = log_dir
        self._on_exit = on_exit

        self._cookie_string = cookie_string
        self._stop_event = ctx.Event()
        self._process: Any = None
        self._watcher: Optional[asyncio.Task] = None
        self._stopped = False
        self._started_at: float = 0.0

    def start(self) -> None:
        """启动子进程与存活监视"""
        self._process = self._ctx.Process(
            target=_room_process_main,
            args=(self.room_id, self.settings, self._queue, self._stop_event,
                  self._log_dir, self._cookie_string),
            name=f"room-{self.room_id}"
        )
        self._process.start()
        self._started_at = time.monotonic()
        self._watcher = asyncio.create_task(self._watch(), name=f"watch-{self.room_id}")
        logger.info(f"房间 {self.room_id} 监控进程已启动 (pid={self._process.pid})")

    @property
    def uptime(self) -> float:
        """自本次启动以来的存活秒数（进程死亡后仍可读取，用于崩溃退避判定）"""
        if self._started_at == 0.0:
            return 0.0
        return time.monotonic() - self._started_at

    @property
    def is_alive(self) -> bool:
        """子进程是否存活"""
        return self._process is not None and self._process.is_alive()

    async def stop(self) -> None:
        """停止子进程（先优雅后强杀）"""
        self._stopped = True
        self._stop_event.set()

        if self._process is not None:
            await asyncio.to_thread(self._process.join, 3)
            if self._process.is_alive():
                self._process.terminate()
                await asyncio.to_thread(self._process.join, 3)
            logger.info(f"房间 {self.room_id} 监控进程已停止")
            self._process = None

        if self._watcher is not None:
            self._watcher.cancel()
            try:
                await self._watcher
            except (asyncio.CancelledError, Exception):
                pass
            self._watcher = None

    async def _watch(self) -> None:
        """存活监视：进程意外退出时触发重建回调（退避逻辑在重建回调内）"""
        proc = self._process
        if proc is None:
            return
        await asyncio.to_thread(proc.join)
        if self._stopped:
            return
        # proc取本地引用读exitcode，规避stop()置None的竞态
        logger.warning(
            f"房间 {self.room_id} 监控进程意外退出 (exitcode={proc.exitcode})")
        if self._on_exit is not None:
            await self._on_exit(self.room_id)


class MonitorManager:
    """
    多房间监控管理器

    主进程不监控直播间：每个房间由独立子进程监控，本管理器保证
    每个roomid只存在一个监控进程，并负责子进程的创建与回收。
    """

    def __init__(
        self,
        settings: Settings,
        bilibili_api: Optional[BilibiliAPI] = None,
        log_dir: Optional[str] = None,
        cookie_pool=None
    ):
        """
        初始化监控管理器

        Args:
            settings: 应用配置
            bilibili_api: 主进程B站API（查询房间信息供命令回复用，默认新建）
            log_dir: 子进程日志目录
            cookie_pool: Cookie账号池（提供后子进程从池中取用登录cookie）
        """
        self.settings = settings
        self.bilibili_api = bilibili_api or BilibiliAPI()
        self.log_dir = log_dir
        self.cookie_pool = cookie_pool

        # spawn上下文：跨平台一致，避免fork继承运行中的事件循环状态
        self._ctx = multiprocessing.get_context("spawn")
        self.push_queue = self._ctx.Queue()

        self._rooms: Dict[int, RoomProcess] = {}
        # 连续快速崩溃计数（房间id→次数）：指数退避与放弃重建的依据
        self._crash_counts: Dict[int, int] = {}
        self._lock = asyncio.Lock()
        # 因登录凭证失效等原因暂停重建的房间（用户重新/监控时自动恢复）
        self._suspended: set = set()
        # 房间亲和账号：{room_id: uid}，重建时优先沿用上次账号
        self._room_account: Dict[int, int] = {}

    async def get_room_info(self, room_id: int) -> LiveRoomInfo:
        """
        获取直播间信息（在线程中执行同步请求，避免阻塞事件循环）

        Args:
            room_id: 直播间ID

        Returns:
            LiveRoomInfo: 直播间信息
        """
        return await asyncio.to_thread(self.bilibili_api.get_room_info, str(room_id))

    async def ensure(self, room_id: int,
                     manual: bool = True) -> Tuple[bool, Optional[LiveRoomInfo]]:
        """
        确保房间的监控进程存在：不存在则创建，已存在则忽略

        Args:
            room_id: 直播间ID
            manual: 是否用户主动触发（主动触发解除暂停并清零崩溃计数；
                崩溃重建回调传入False以保留退避计数）

        Returns:
            Tuple[bool, Optional[LiveRoomInfo]]:
                (是否新建了监控进程, 直播间信息，获取失败时为None)
        """
        rid = int(room_id)

        async with self._lock:
            if manual:
                # 用户主动确保监控：解除暂停；显式ensure视为新的监控意图，崩溃计数清零
                self._suspended.discard(rid)
                self._crash_counts.pop(rid, None)
            existing = self._rooms.get(rid)
            if existing is not None and existing.is_alive:
                logger.info(f"房间 {rid} 已存在监控进程，忽略")
                return False, None

            try:
                room_info = await self.get_room_info(rid)
            except Exception as e:
                logger.error(f"获取房间 {rid} 信息失败: {e}")
                room_info = None

            # 从账号池取用登录cookie（房间亲和优先，池空则游客连接）
            account = await self._pick_account(rid)
            cookie_uid = account.uid if account else 0
            cookie_string = account.cookie_string if account else ""

            room = RoomProcess(
                room_id=rid,
                settings=self.settings,
                queue=self.push_queue,
                ctx=self._ctx,
                log_dir=self.log_dir,
                on_exit=self._rebuild,
                cookie_uid=cookie_uid,
                cookie_string=cookie_string
            )
            room.start()
            self._rooms[rid] = room
            return True, room_info

    async def _rebuild(self, room_id: int) -> None:
        """重建意外退出的房间监控进程（watcher回调；暂停中的房间跳过）。

        连续快速崩溃按指数退避（REBUILD_DELAY起步、MAX_REBUILD_DELAY封顶）；
        上次进程稳定运行超过STABLE_UPTIME秒则计数重置；连续超过
        MAX_CONSECUTIVE_CRASHES次放弃自动重建（用户重新ensure可恢复）。
        """
        rid = int(room_id)
        if rid in self._suspended:
            logger.info(f"房间 {rid} 处于暂停状态，跳过重建")
            return

        async with self._lock:
            room = self._rooms.get(rid)
            if room is None or room.is_alive:
                return
            uptime = room.uptime
            self._rooms.pop(rid, None)

        if uptime >= STABLE_UPTIME:
            # 上次进程稳定运行过一段时间，本次视作偶发崩溃重新计数
            self._crash_counts.pop(rid, None)
        crashes = self._crash_counts.get(rid, 0) + 1
        self._crash_counts[rid] = crashes
        if crashes > MAX_CONSECUTIVE_CRASHES:
            logger.error(
                f"房间 {rid} 监控进程连续快速崩溃{MAX_CONSECUTIVE_CRASHES}次，"
                f"放弃自动重建；重新监控（群指令/面板）可恢复"
            )
            return
        delay = min(REBUILD_DELAY * (2 ** (crashes - 1)), MAX_REBUILD_DELAY)
        if delay > REBUILD_DELAY:
            logger.warning(f"房间 {rid} 连续快速崩溃{crashes}次，退避{delay}秒后重建")

        await asyncio.sleep(delay)
        logger.info(f"重建房间 {rid} 监控进程")
        try:
            await self.ensure(rid, manual=False)
        except Exception as e:
            logger.error(f"重建房间 {rid} 监控进程失败: {e}")

    async def suspend(self, room_id: int, reason: str = "") -> bool:
        """
        暂停房间的监控与自动重建（如登录凭证失效）

        Args:
            room_id: 直播间ID
            reason: 暂停原因（日志用）

        Returns:
            bool: 是否有存活的监控进程被停止
        """
        rid = int(room_id)

        async with self._lock:
            room = self._rooms.pop(rid, None)
            self._suspended.add(rid)

        if room is not None:
            await room.stop()

        logger.warning(f"房间 {rid} 监控已暂停: {reason or '未知原因'}")
        return room is not None

    @property
    def suspended_room_ids(self) -> List[int]:
        """处于暂停状态的房间列表"""
        return sorted(self._suspended)

    async def remove(self, room_id: int) -> bool:
        """
        停止并移除房间的监控进程

        Args:
            room_id: 直播间ID

        Returns:
            bool: 是否存在监控进程并被停止
        """
        rid = int(room_id)

        async with self._lock:
            room = self._rooms.pop(rid, None)

        if room is None:
            return False

        await room.stop()
        return True

    def has(self, room_id: int) -> bool:
        """房间是否存在存活的监控进程"""
        room = self._rooms.get(int(room_id))
        return room is not None and room.is_alive

    def get_cookie_uid(self, room_id: int) -> int:
        """房间监控进程使用的账号池账号UID（0表示未用账号池）"""
        room = self._rooms.get(int(room_id))
        return room.cookie_uid if room is not None else 0

    def get_account_rooms(self, uid: int) -> list:
        """反向查询：使用指定账号的房间列表（账号亲和映射）"""
        return sorted(rid for rid, account_uid in self._room_account.items()
                      if account_uid == int(uid))

    async def _pick_account(self, room_id: int):
        """
        为房间选择账号：亲和优先（上次使用的账号仍active则沿用），
        否则LRU轮转取用；结果记录为该房间的新亲和账号

        Args:
            room_id: 直播间ID

        Returns:
            Optional[CookieAccount]: 账号，池空返回None
        """
        if self.cookie_pool is None:
            return None

        rid = int(room_id)
        # 亲和：上次使用的账号仍可用则沿用，避免房间频繁换号
        prev_uid = self._room_account.get(rid)
        if prev_uid is not None:
            account = await self.cookie_pool.acquire_specific(prev_uid)
            if account is not None:
                return account

        account = await self.cookie_pool.acquire()
        if account is not None:
            self._room_account[rid] = account.uid
        else:
            self._room_account.pop(rid, None)
        return account

    def room_ids(self) -> List[int]:
        """当前监控的房间列表"""
        return list(self._rooms.keys())

    def rooms(self) -> Dict[int, "RoomProcess"]:
        """当前全部房间进程（副本）"""
        return dict(self._rooms)

    async def restore(self, room_ids: List[int]) -> int:
        """
        启动时恢复监控进程

        Args:
            room_ids: 需要恢复监控的直播间ID列表

        Returns:
            int: 成功恢复的房间数
        """
        restored = 0
        for rid in room_ids:
            try:
                await self.ensure(rid)
                restored += 1
            except Exception as e:
                logger.error(f"恢复房间 {rid} 监控失败: {e}")
        if restored:
            logger.info(f"已恢复 {restored} 个房间的监控进程")
        return restored

    async def stop_all(self) -> None:
        """停止全部监控进程（并行停关：remove锁内仅pop，慢速join在锁外）"""
        rids = list(self._rooms.keys())
        if not rids:
            return
        await asyncio.gather(
            *(self.remove(rid) for rid in rids),
            return_exceptions=True,
        )
