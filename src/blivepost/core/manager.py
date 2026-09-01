# -*- coding: utf-8 -*-
"""
多房间监控管理

主进程不监控直播间：每个房间由独立的子进程连接弹幕服务器并处理事件，
子进程产生的推送与落库请求经跨进程队列发回主进程统一处理
（推送按绑定关系路由，落库集中写入避免SQLite多进程写冲突）。

- RoomProcess：单个房间的子进程封装，进程死亡由watcher即时重建
- MonitorManager：管理全部房间子进程，保证每个roomid只有一个监控进程
- QueuePushService/QueueDbBridge：子进程侧的推送/落库桥
- PushQueueConsumer：主进程侧的队列消费者
"""

import asyncio
import multiprocessing
import os
import queue
from datetime import datetime

import aiohttp
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from ..config.settings import Settings
from ..models.live import LiveRoomInfo, LiveStats, TempData
from ..services.bilibili import BilibiliAPI
from ..services.push import PushService, convert_cq_message
from ..utils.logger import get_logger
from ..vendor.importer import import_blivedm
from ..vendor.patch import apply_patch
from .handler import create_handler_with_base


if TYPE_CHECKING:
    from ..database import DatabaseService


logger = get_logger("manager")

# 子进程内直播状态校正间隔（秒）
RECONCILE_INTERVAL = 90
# 子进程异常退出后的重建延迟（秒），避免快速崩溃循环
REBUILD_DELAY = 5


# ==================== 子进程侧 ====================


class QueueDbBridge:
    """
    子进程落库桥

    以DatabaseService的同名save_*异步方法为接口，把落库请求
    序列化后写入跨进程队列，由主进程集中执行。
    """

    def __init__(self, queue):
        self._queue = queue
        # 与DatabaseService同名save_*接口（鸭子类型）

    def __getattr__(self, name: str):
        if not name.startswith("save_"):
            raise AttributeError(name)

        async def _save(**kwargs) -> None:
            await asyncio.to_thread(
                self._queue.put,
                {"type": "db", "method": name, "kwargs": kwargs}
            )

        return _save


class QueuePushService(PushService):
    """
    子进程推送服务

    所有消息经CQ转换后写入跨进程队列，由主进程按绑定关系路由推送。
    开播消息携带封面URL，主进程优先走富媒体推送。
    """

    def __init__(self, settings: Settings, queue, room_id: int):
        super().__init__(settings)
        self._queue = queue
        self.room_id = room_id

    def send_message(self, message: str, room_id=None) -> bool:
        return self._forward(message, media=None)

    def push_live_start(self, room_info: LiveRoomInfo) -> bool:
        return self._forward(
            self._live_start_text(room_info),
            media=self._live_start_image(room_info)
        )

    def push_live_end(self, room_info: LiveRoomInfo, stats: LiveStats) -> bool:
        return self._forward(
            self._live_end_text(room_info, stats),
            media=room_info.cover or None
        )

    def notify_not_login(self, room_id) -> bool:
        """登录凭证失效上报：经队列发回主进程处理"""
        try:
            self._queue.put(
                {"type": "not_login", "room_id": int(room_id)},
                timeout=5
            )
            return True
        except Exception as e:
            logger.error(f"凭证失效上报写入队列失败: {e}")
            return False

    def _forward(self, message: str, media: Optional[str]) -> bool:
        content = convert_cq_message(message)
        if not content:
            logger.warning("消息内容为空，跳过发送")
            return False
        try:
            self._queue.put(
                {"type": "push", "room_id": self.room_id, "content": content, "media": media},
                timeout=5
            )
            return True
        except Exception as e:
            logger.error(f"推送请求写入队列失败: {e}")
            return False


async def _reconcile_loop(
    room_id: int,
    api: BilibiliAPI,
    handler_ref: Callable,
    stop_event
) -> None:
    """
    子进程内的直播状态校正循环

    弹幕连接抖动可能错过LIVE/PREPARING事件，定期用REST接口
    核对真实直播状态并合成缺失的开播/下播事件。
    """
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(asyncio.to_thread(stop_event.wait), timeout=RECONCILE_INTERVAL)
            break  # stop_event被置位
        except asyncio.TimeoutError:
            pass

        try:
            handler = handler_ref()
            if handler is None:
                continue
            status = await asyncio.to_thread(api.get_live_status, str(room_id))
            if status is None:
                continue
            if status and not handler.is_live:
                logger.warning(f"房间 {room_id} 实际已开播但未收到开播事件，合成开播处理")
                handler._on_live_start(None, None)
            elif not status and handler.is_live:
                logger.warning(f"房间 {room_id} 实际已下播但未收到下播事件，合成下播处理")
                handler._on_live_preparing(None, None)
        except Exception as e:
            logger.error(f"房间 {room_id} 状态校正失败: {e}")


def _build_cookie_session(cookie_string: str):
    """
    构建携带B站登录cookie的aiohttp会话（弹幕连接登录用）

    Args:
        cookie_string: 完整cookie字符串

    Returns:
        aiohttp.ClientSession
    """
    import http.cookies

    cookies = http.cookies.SimpleCookie()
    # 弹幕连接登录只需SESSDATA
    sessdata = ""
    for part in cookie_string.split(";"):
        part = part.strip()
        if part.startswith("SESSDATA="):
            sessdata = part[len("SESSDATA="):]
            break
    if not sessdata:
        return None
    cookies["SESSDATA"] = sessdata
    cookies["SESSDATA"]["domain"] = ".bilibili.com"

    session = aiohttp.ClientSession()
    session.cookie_jar.update_cookies(cookies)
    return session


async def _room_process_async(
    room_id: int,
    settings: Settings,
    push_queue,
    stop_event,
    cookie_string: str = ""
) -> None:
    """子进程异步主函数：连接弹幕服务器并处理事件"""
    logger = get_logger("room")
    try:
        from ..vendor import setup_blivedm
        setup_blivedm()
    except Exception as e:
        logger.warning(f"blivedm初始化失败: {e}")

    apply_patch(settings.message.ignore_cmds)
    blivedm = import_blivedm()

    push_service = QueuePushService(settings, push_queue, room_id)
    push_service.bilibili_api = BilibiliAPI()
    handler = create_handler_with_base(
        settings, push_service, LiveStats(), TempData(), str(room_id), QueueDbBridge(push_queue)
    )

    session = _build_cookie_session(cookie_string) if cookie_string else None
    if session is not None:
        logger.info(f"房间 {room_id} 使用账号池cookie连接弹幕服务器")
    client = blivedm.BLiveClient(room_id, session=session)
    client.set_handler(handler)
    client.start()
    logger.info(f"房间 {room_id} 监控进程已启动 (pid={os.getpid()})")

    join_task = asyncio.create_task(client.join(), name=f"join-{room_id}")
    stop_task = asyncio.create_task(
        asyncio.to_thread(stop_event.wait), name=f"stop-{room_id}"
    )
    reconcile_task = asyncio.create_task(
        _reconcile_loop(room_id, push_service.bilibili_api, lambda: handler, stop_event)
    )

    done, _ = await asyncio.wait({join_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

    if stop_task in done and not join_task.done():
        # 主进程要求停止
        logger.info(f"房间 {room_id} 收到停止信号")
        try:
            await client.stop_and_close()
        except Exception as e:
            logger.warning(f"房间 {room_id} 停止客户端出错: {e}")
    elif join_task in done:
        # 弹幕连接自行退出，进程结束，由主进程watcher重建
        exc = join_task.exception()
        logger.warning(f"房间 {room_id} 弹幕连接退出: {exc}")

    reconcile_task.cancel()


def _room_process_main(room_id: int, settings: Settings, push_queue, stop_event,
                       log_dir=None, cookie_string: str = "") -> None:
    """子进程入口（需保持模块级以便spawn导入）"""
    from ..utils.logger import Logger

    Logger().setup(name="blivepost", log_dir=log_dir, console=True)
    asyncio.run(_room_process_async(room_id, settings, push_queue, stop_event, cookie_string))


# ==================== 主进程侧 ====================


class PushQueueConsumer:
    """
    跨进程队列消费者（主进程）

    消费子进程发来的推送与落库请求：推送经PushService按绑定关系
    路由（优先富媒体），落库集中写入数据库。
    """

    def __init__(
        self,
        queue,
        push_service: PushService,
        db_service: Optional["DatabaseService"] = None,
        monitor_manager: Optional["MonitorManager"] = None,
        cookie_pool=None,
        admin_openids: Optional[list] = None,
        main_admin_openids: Optional[list] = None,
        binding_store=None
    ):
        """
        初始化消费者

        Args:
            queue: 跨进程队列（MonitorManager.push_queue）
            push_service: 主进程推送服务
            db_service: 数据库存储服务
            monitor_manager: 监控管理器（处理子进程上报时暂停/重建用）
            cookie_pool: Cookie账号池（凭证失效轮转用）
            admin_openids: 管理员openid列表（私聊提醒用）
        """
        self._queue = queue
        self.push_service = push_service
        self.db_service = db_service
        self.monitor_manager = monitor_manager
        self.cookie_pool = cookie_pool
        self.admin_openids = [str(o) for o in (admin_openids or [])]
        # 主管理员：接收Cookie失效等敏感通知，未配置时回落全部管理员
        self.main_admin_openids = [str(o) for o in (main_admin_openids or [])]
        self.binding_store = binding_store
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    def start(self) -> None:
        """启动消费循环（需在事件循环内调用）"""
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self.run(), name="push-queue-consumer")
            logger.info("子进程消息消费者已启动")

    async def stop(self) -> None:
        """停止消费（置停止标志，等待循环在轮询间隙退出）"""
        self._stopping = True
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
            if not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None

    async def run(self) -> None:
        """消费主循环（带超时轮询，避免线程无限阻塞在队列上）"""
        while True:
            try:
                item = await asyncio.to_thread(self._queue.get, True, 1)
            except queue.Empty:
                if self._stopping:
                    break
                continue
            if item is None:
                break
            try:
                await self._handle(item)
            except Exception as e:
                logger.error(f"处理子进程消息失败: {e}")

    async def _handle(self, item: dict) -> None:
        """处理单条队列消息"""
        kind = item.get("type")

        if kind == "push":
            room_id = item.get("room_id")
            media = item.get("media")
            content = item.get("content", "")
            if media:
                self.push_service.push_media_message(media, content, room_id=room_id)
            else:
                self.push_service.send_message(content, room_id=room_id)

        elif kind == "not_login":
            await self._handle_not_login(item.get("room_id"))

        elif kind == "dynamic":
            await self._handle_dynamic(item)

        elif kind == "up_live":
            await self._handle_up_live(item)

        elif kind == "db":
            method = getattr(self.db_service, item["method"], None) if self.db_service else None
            if method is None:
                logger.error(f"落库方法不存在: {item['method']}")
                return
            await method(**item["kwargs"])

        else:
            logger.warning(f"未知队列消息类型: {kind}")

    async def _handle_not_login(self, room_id) -> None:
        """
        处理子进程上报的登录凭证失效

        标记账号失效并私聊提醒管理员（不在群里发送），随后从账号池
        轮转其他账号重建监控；池中无可用账号时暂停监控。
        """
        rid = int(room_id) if room_id is not None else 0
        uid = self.monitor_manager.get_cookie_uid(rid) if self.monitor_manager else 0
        logger.error(f"房间 {rid} 上报B站登录凭证失效 (账号uid={uid})")

        if self.cookie_pool is not None and uid:
            await self.cookie_pool.report_invalid(uid, "弹幕连接报告登录失效")

        await self._notify_admins(
            f"⚠️ 账号 {uid} 的B站登录凭证已失效（直播间{rid}）", sensitive=True
        )

        if self.monitor_manager is None:
            return

        # 移除使用失效凭证的监控进程
        await self.monitor_manager.remove(rid)

        # 账号池还有可用账号则轮转重建，否则暂停
        if self.cookie_pool is not None and self.cookie_pool.active_accounts():
            await self.monitor_manager.ensure(rid)
            logger.info(f"直播间 {rid} 已轮转账号重启监控")
            warn = self.cookie_pool.warn_if_needed()
            if warn:
                await self._notify_admins(f"⚠️ {warn}", sensitive=True)
            return

        await self.monitor_manager.suspend(rid, reason="登录凭证失效且账号池无可用账号")
        await self._notify_admins(
            f"账号池无可用账号，直播间{rid}的监控已暂停。\n"
            f"请私聊机器人发送 /登录 添加账号后，@机器人 发送 /监控 {rid} 恢复。"
        )

    async def _handle_dynamic(self, item: dict) -> None:
        """处理子进程上报的新动态：推送到所有订阅该UP主的群"""
        up_uid = item.get("up_uid")
        dynamic_id = item.get("dynamic_id", "")
        author = item.get("author") or (
            self.binding_store.get_dynamic_up_name(up_uid)
            if self.binding_store else "") or f"UP主{up_uid}"
        from datetime import datetime as _dt
        pub_ts = item.get("pub_ts") or 0
        pub_time = _dt.fromtimestamp(pub_ts).strftime("%Y-%m-%d %H:%M") if pub_ts else "刚刚"

        # 回填UP主昵称
        if self.binding_store is not None and item.get("author"):
            await self.binding_store.update_dynamic_up_name(up_uid, item["author"])

        content = (
            f"{author} 发布新动态\n"
            f"时间：{pub_time}\n"
            f"链接：https://www.bilibili.com/opus/{dynamic_id}"
        )
        summary = (item.get("summary") or "").strip()
        if summary:
            content += f"\n内容：{summary}"
        groups = self.binding_store.get_dynamic_groups(up_uid) if self.binding_store else []
        if not groups:
            logger.info(f"UP主 {up_uid} 的新动态无订阅群，跳过推送")
            return
        bot = getattr(self.push_service, "bot_client", None)
        if bot is None:
            logger.warning("机器人未启动，动态推送失败")
            return
        # 带图动态走富媒体（单群失败自动回退文本），纯文本直接广播
        image = (item.get("image") or "").strip()
        if image:
            bot.push_media(image, content, groups)
        else:
            await bot.broadcast(groups, content)
        logger.info(f"UP主 {up_uid} 新动态已推送到 {len(groups)} 个群")

    async def _handle_up_live(self, item: dict) -> None:
        """处理开播提醒：推送到所有订阅该UP主动态的群"""
        up_uid = item.get("up_uid")
        author = (self.binding_store.get_dynamic_up_name(up_uid)
                  if self.binding_store else "") or f"UP主{up_uid}"
        title = item.get("title") or ""
        room_id = item.get("room_id") or 0

        content = f"{author} 开播啦！\n标题：{title}\n链接：https://live.bilibili.com/{room_id}"
        groups = self.binding_store.get_dynamic_groups(up_uid) if self.binding_store else []
        if not groups:
            return
        bot = getattr(self.push_service, "bot_client", None)
        if bot is None:
            logger.warning("机器人未启动，开播提醒推送失败")
            return
        await bot.broadcast(groups, content)
        logger.info(f"UP主 {up_uid} 开播提醒已推送到 {len(groups)} 个群")

    async def _notify_admins(self, content: str, sensitive: bool = False) -> None:
        """
        私聊提醒管理员（Cookie相关敏感信息不进群）

        Args:
            content: 通知内容
            sensitive: 敏感通知发主管理员（未配置主管理员时发全部管理员）
        """
        targets = self.admin_openids
        if sensitive and self.main_admin_openids:
            targets = self.main_admin_openids
        if not targets:
            logger.info(f"(无管理员配置，仅记录) {content}")
            return
        bot = getattr(self.push_service, "bot_client", None)
        if bot is None:
            logger.warning(f"机器人未启动，无法私聊提醒管理员: {content}")
            return
        await bot.notify_admins(targets, content)


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

    def start(self) -> None:
        """启动子进程与存活监视"""
        self._process = self._ctx.Process(
            target=_room_process_main,
            args=(self.room_id, self.settings, self._queue, self._stop_event,
                  self._log_dir, self._cookie_string),
            name=f"room-{self.room_id}"
        )
        self._process.start()
        self._watcher = asyncio.create_task(self._watch(), name=f"watch-{self.room_id}")
        logger.info(f"房间 {self.room_id} 监控进程已启动 (pid={self._process.pid})")

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
        """存活监视：进程意外退出时延迟后触发重建回调"""
        await asyncio.to_thread(self._process.join)
        if self._stopped:
            return
        logger.warning(
            f"房间 {self.room_id} 监控进程意外退出 (exitcode={self._process.exitcode})，"
            f"{REBUILD_DELAY}秒后重建"
        )
        await asyncio.sleep(REBUILD_DELAY)
        if self._stopped:
            return
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

    async def ensure(self, room_id: int) -> Tuple[bool, Optional[LiveRoomInfo]]:
        """
        确保房间的监控进程存在：不存在则创建，已存在则忽略

        Args:
            room_id: 直播间ID

        Returns:
            Tuple[bool, Optional[LiveRoomInfo]]:
                (是否新建了监控进程, 直播间信息，获取失败时为None)
        """
        rid = int(room_id)

        async with self._lock:
            # 用户主动确保监控时解除暂停
            self._suspended.discard(rid)
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
        """重建意外退出的房间监控进程（watcher回调；暂停中的房间跳过）"""
        rid = int(room_id)
        if rid in self._suspended:
            logger.info(f"房间 {rid} 处于暂停状态，跳过重建")
            return

        async with self._lock:
            room = self._rooms.get(rid)
            if room is None or room.is_alive:
                return
            self._rooms.pop(rid, None)

        logger.info(f"重建房间 {rid} 监控进程")
        try:
            await self.ensure(rid)
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
        """停止全部监控进程"""
        for rid in list(self._rooms.keys()):
            await self.remove(rid)


class CookieMaintainer:
    """
    Cookie维护循环（主进程）

    周期检查账号池：临近过期的账号用无头浏览器载入刷新（续期
    SESSDATA与浏览器票据），刷新失败且nav验证失效的账号标记为
    失效并私聊提醒管理员（不在群里发送）。
    """

    def __init__(
        self,
        cookie_pool,
        bot_client=None,
        admin_openids: Optional[list] = None,
        main_admin_openids: Optional[list] = None,
        interval: int = 6 * 3600,
        db_service=None,
        db_size_warn_mb: int = 500
    ):
        """
        初始化维护循环

        Args:
            cookie_pool: Cookie账号池
            bot_client: QQ机器人客户端（私聊提醒用，可后续设置）
            admin_openids: 管理员openid列表
            main_admin_openids: 主管理员列表（敏感通知，未配置时发全部管理员）
            interval: 检查间隔（秒）
            db_service: 数据库服务（体积监控用）
            db_size_warn_mb: 数据库体积预警阈值（MB）
        """
        self.pool = cookie_pool
        self.bot_client = bot_client
        self.admin_openids = [str(o) for o in (admin_openids or [])]
        self.main_admin_openids = [str(o) for o in (main_admin_openids or [])]
        self.interval = interval
        self._last_rescan_date: str = ""
        # 数据量监控（永久保留策略下的增长提醒）
        self.db_service = db_service
        self.db_size_warn_mb = db_size_warn_mb
        self._db_warned = False
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """启动维护循环（需在事件循环内调用）"""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="cookie-maintainer")
            logger.info(f"Cookie维护循环已启动（间隔{self.interval // 3600}小时）")

    async def stop(self) -> None:
        """停止维护循环"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def run(self) -> None:
        """维护主循环"""
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.check_once()
            except Exception as e:
                logger.error(f"Cookie维护检查失败: {e}")

    async def check_once(self) -> None:
        """执行一轮维护：临近过期刷新、失效标记并提醒管理员"""
        due = self.pool.refresh_due_accounts()
        if not due:
            return

        # 惰性导入避免未安装playwright时影响其他功能
        from ..services.bili_login import BiliLoginService, verify_nav

        service = BiliLoginService()
        for account in due:
            logger.info(f"刷新账号 {account.uid}（{account.nickname}）的cookie...")
            try:
                result = await service.refresh(account.cookie_string)
            except Exception as e:
                logger.warning(f"账号 {account.uid} cookie刷新异常: {e}")
                continue

            if result.ok and result.uid == account.uid:
                await self.pool.upsert(
                    uid=result.uid,
                    nickname=result.nickname,
                    cookie_string=result.cookie_string,
                    sessdata=result.sessdata,
                    bili_jct=result.bili_jct,
                    ac_time_value=result.ac_time_value,
                    sessdata_expires_at=result.sessdata_expires_at,
                )
                continue

            # 刷新失败：nav复核登录态
            ok, _, _ = verify_nav(account.cookie_string)
            if ok:
                logger.warning(
                    f"账号 {account.uid} 刷新失败但登录态仍有效，等待下轮: {result.error}"
                )
                continue

            reason = result.error or "维护刷新失败且登录态已失效"
            await self.pool.report_invalid(account.uid, reason)
            await self._notify_admins(
                f"⚠️ 账号 {account.uid}（{account.nickname}）的Cookie已失效并移出可用池。\n"
                f"原因：{reason}\n请私聊机器人发送 /登录 重新扫码添加。",
                sensitive=True,
            )
            warn = self.pool.warn_if_needed()
            if warn:
                await self._notify_admins(f"⚠️ {warn}", sensitive=True)

        # 失效账号自动重扫：nav复核通过则自动回池（如临时风控恢复场景，每日一次）
        await self._rescan_expired()

        # 数据库体积检查（永久保留策略下的增长监控，提醒而非删除）
        if self.db_size_warn_mb > 0:
            await self._check_db_size(self.db_size_warn_mb)

        # 数据库体积检查（永久保留策略下的增长监控，提醒而非删除）
        await self._check_db_size()

    async def _check_db_size(self, warn_mb: int = 500) -> None:
        """数据库体积超阈值时私聊提醒管理员（只提醒不删除，符合永久保留决策）"""
        db_service = self.db_service
        # db_path在DatabaseManager上（DatabaseService.db_manager转发）
        db_manager = getattr(db_service, "db_manager", None)
        path = getattr(db_manager, "db_path", None) if db_manager else None
        if not path or not os.path.exists(path):
            return
        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb < warn_mb:
            self._db_warned = False
            return
        if getattr(self, "_db_warned", False):
            return  # 已提醒过且未恢复，不重复
        self._db_warned = True
        msg1 = (
            "⚠️ 数据库体积已达 " + str(round(size_mb, 1)) + "MB（阈值" + str(warn_mb) + "MB）。"
        )
        msg2 = "数据按策略永久保留，不会自动删除；建议导出后归档，或考虑迁移到MySQL/PostgreSQL。"
        await self._notify_admins(msg1 + msg2, sensitive=True)

    async def _rescan_expired(self) -> int:
        """
        nav复核全部expired账号，登录态恢复的自动回到可用池（每日一次）

        Returns:
            int: 恢复的账号数量
        """
        expired = [a for a in self.pool.accounts if a.status == "expired"]
        if not expired:
            return 0

        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_rescan_date == today:
            return 0
        self._last_rescan_date = today

        from ..services.bili_login import verify_nav
        restored = 0
        for account in expired:
            ok, _, uname = await asyncio.to_thread(verify_nav, account.cookie_string)
            if ok:
                await self.pool.reactivate(account.uid, reason="自动重扫发现登录态恢复")
                restored += 1
                logger.info(f"账号 {account.uid}（{uname or account.nickname}）"
                            f"重扫通过，已自动恢复可用")
                await self._notify_admins(
                    f"✅ 账号 {account.uid}（{uname or account.nickname}）"
                    f"重扫发现登录态恢复，已自动回到可用池"
                )
        if restored:
            logger.info(f"自动重扫恢复 {restored} 个账号")
        return restored

    async def _notify_admins(self, content: str, sensitive: bool = False) -> None:
        """
        私聊提醒管理员

        Args:
            content: 通知内容
            sensitive: 敏感通知发主管理员（未配置时发全部管理员）
        """
        targets = self.admin_openids
        if sensitive and self.main_admin_openids:
            targets = self.main_admin_openids
        if not targets:
            logger.info(f"(无管理员配置，仅记录) {content}")
            return
        if self.bot_client is None:
            logger.warning(f"机器人未启动，无法私聊提醒管理员: {content}")
            return
        await self.bot_client.notify_admins(targets, content)
