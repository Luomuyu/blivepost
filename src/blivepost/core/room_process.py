# -*- coding: utf-8 -*-
"""
子进程侧：房间监控子进程的装配与运行

子进程连接弹幕服务器并处理事件，产生的推送与落库请求经跨进程
队列发回主进程统一处理（见manager.py的PushQueueConsumer）。

- QueueDbBridge/QueuePushService：子进程侧的推送/落库桥
- _room_process_main：子进程入口（spawn目标，需保持模块级可pickle）
"""

import asyncio
import os

import aiohttp
from typing import Callable, Optional

from ..config.settings import Settings
from ..models.live import LiveRoomInfo, LiveStats, TempData
from ..services.bilibili import BilibiliAPI
from ..services.push import PushService, convert_cq_message, live_enter_keyboard
from ..utils.logger import get_logger
from ..vendor.importer import import_blivedm
from ..vendor.patch import apply_patch
from .handler import create_handler_with_base


logger = get_logger("manager")

# 子进程内直播状态校正间隔（秒）
RECONCILE_INTERVAL = 90


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

    def send_message(self, message: str, room_id=None,
                     keyboard: Optional[dict] = None) -> bool:
        return self._forward(message, media=None, keyboard=keyboard)

    def push_live_start(self, room_info: LiveRoomInfo) -> bool:
        """开播推送经队列转发（部件+按钮），主进程侧执行完整降级链"""
        try:
            self._queue.put(
                {"type": "push_live_start", "room_id": self.room_id,
                 "content": self._live_start_text(room_info),
                 "media": self._live_start_image(room_info),
                 "keyboard": live_enter_keyboard(room_info.room_id)},
                timeout=5
            )
            return True
        except Exception as e:
            logger.error(f"开播推送写入队列失败: {e}")
            return False

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

    def _forward(self, message: str, media: Optional[str],
                 keyboard: Optional[dict] = None) -> bool:
        content = convert_cq_message(message)
        if not content:
            logger.warning("消息内容为空，跳过发送")
            return False
        try:
            self._queue.put(
                {"type": "push", "room_id": self.room_id, "content": content,
                 "media": media, "keyboard": keyboard,
                 "switch": self._switch_tag},
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
        # 注意不可用to_thread(stop_event.wait)+wait_for：线程内无超时的
        # wait在超时后仍永久阻塞，每轮泄漏一个执行器线程，最终耗尽线程池
        # 使本进程所有to_thread调用（状态查询/落库）永久挂起
        await asyncio.sleep(RECONCILE_INTERVAL)
        if stop_event.is_set():
            break

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


async def _parent_watch(interval: float = 30.0) -> None:
    """父进程存活监视：父进程消失时返回（驱动主循环退出，防泄漏）"""
    import multiprocessing
    parent = multiprocessing.parent_process()
    if parent is None:
        # 非spawn场景（直接运行本模块）无父进程概念，挂起等取消即可
        await asyncio.Event().wait()
        return
    while parent.is_alive():
        await asyncio.sleep(interval)


async def _wait_stop_event(stop_event, poll: float = 1.0) -> None:
    """等待停止信号（纯异步轮询，poll秒粒度）

    不可用asyncio.to_thread(stop_event.wait)：线程内无超时的wait会永久
    占用执行器线程——主协程因join/parent先完成而返回后，asyncio.run在
    收尾shutdown_default_executor时join该线程永久挂起，子进程变僵尸
    （父进程watcher等不到退出，永不重建）。mp.Event.is_set()开销极低，
    1秒轮询无感。
    """
    while not stop_event.is_set():
        await asyncio.sleep(poll)


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
        _wait_stop_event(stop_event), name=f"stop-{room_id}"
    )
    # 父进程存活自检：主进程被强杀时stop_event不会置位、子进程泄漏续跑
    # （2026-11-10真机累积多个泄漏进程）——父进程消失则自行退出
    parent_task = asyncio.create_task(
        _parent_watch(), name=f"parent-watch-{room_id}"
    )
    reconcile_task = asyncio.create_task(
        _reconcile_loop(room_id, push_service.bilibili_api, lambda: handler, stop_event)
    )

    done, _ = await asyncio.wait(
        {join_task, stop_task, parent_task}, return_when=asyncio.FIRST_COMPLETED)

    if parent_task in done and not stop_task.done():
        logger.warning(f"房间 {room_id} 父进程已退出，子进程自动结束（防泄漏）")

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
    if not parent_task.done():
        parent_task.cancel()


def _room_process_main(room_id: int, settings: Settings, push_queue, stop_event,
                       log_dir=None, cookie_string: str = "") -> None:
    """子进程入口（需保持模块级以便spawn导入）"""
    from ..utils.logger import Logger

    # 独立日志文件（blivepost_room_<id>.<日期>.log）：与父进程及其他房间
    # 子进程分离，避免多进程同写一文件在Windows轮转时PermissionError丢日志
    Logger().setup(name="blivepost", log_dir=log_dir, console=True,
                   log_suffix=f"room_{room_id}")
    asyncio.run(_room_process_async(room_id, settings, push_queue, stop_event, cookie_string))
