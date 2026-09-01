# -*- coding: utf-8 -*-
"""
B站动态监控（子进程轮询）

每个被订阅的UP主对应一个独立子进程，轮询动态feed接口：
发现新动态后经跨进程队列上报主进程，主进程推送到所有订阅该
UP主的群（复用绑定路由）。子进程意外退出由watcher即时重建；
最后一个订阅群取消后停止轮询进程。
"""

import asyncio
import json
import multiprocessing
import os
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..config.settings import Settings
from ..utils.logger import get_logger


logger = get_logger("dynamics")

DYNAMIC_API_BASE = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
LIVE_STATE_FILENAME = "blivepost_dynamic_live_state.json"
HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
}
POLL_MIN, POLL_MAX = 60, 90  # 轮询间隔随机范围（秒）
REBUILD_DELAY = 5


def _build_dynamic_url(up_uid: int, wbi=None) -> str:
    """
    构造动态feed接口URL（带WBI签名；签名不可用时降级为无签名URL）

    Args:
        up_uid: UP主UID
        wbi: WbiSigner实例（None时尝试新建）

    Returns:
        str: 完整URL
    """
    params = f"host_mid={up_uid}&platform=web"
    try:
        if wbi is None:
            from .wbi import WbiSigner
            wbi = WbiSigner()
        w_rid, wts = wbi.sign_url_params(params)
        if w_rid:
            return f"{DYNAMIC_API_BASE}?{params}&wts={wts}&w_rid={w_rid}"
    except Exception as e:
        logger.warning(f"WBI签名失败，使用无签名URL: {e}")
    return f"{DYNAMIC_API_BASE}?{params}"


def _extract_content(modules: dict) -> Tuple[str, str]:
    """
    防御式提取动态内容摘要与首图（覆盖图文/投稿视频/专栏/转发等类型）

    Args:
        modules: 动态item的modules节点

    Returns:
        Tuple[str, str]: (摘要, 首图URL)，取不到为空串
    """
    md = modules.get("module_dynamic") or {}
    if not isinstance(md, dict):
        md = {}
    major = md.get("major") or {}
    if not isinstance(major, dict):
        major = {}

    # 文本：普通动态 -> 视频描述 -> 专栏标题/摘要 -> 转发者附言
    summary = ""
    desc = md.get("desc") or {}
    if isinstance(desc, dict):
        summary = (desc.get("text") or "").strip()
    if not summary:
        archive = major.get("archive") or {}
        if isinstance(archive, dict):
            summary = (archive.get("desc") or archive.get("title") or "").strip()
    if not summary:
        opus = major.get("opus") or {}
        if isinstance(opus, dict):
            summary = (opus.get("title") or opus.get("desc") or "").strip()

    # 转发动态：附言之外补被转发内容的摘要
    orig = None
    for key in ("module_static",):
        node = modules.get(key) or {}
        if isinstance(node, dict):
            orig = node.get("module_dynamic") or {}
            break
    if orig and isinstance(orig, dict) and len(summary) < 40:
        orig_desc = orig.get("desc") or {}
        if isinstance(orig_desc, dict):
            orig_text = (orig_desc.get("text") or "").strip()
            if orig_text:
                summary = (summary + "\n转发：" + orig_text[:40]).strip()

    # 图：图文draw首图 -> 视频封面 -> 专栏配图
    image = ""
    draw = major.get("draw") or {}
    if isinstance(draw, dict):
        draw_items = draw.get("items") or []
        if draw_items and isinstance(draw_items[0], dict):
            image = draw_items[0].get("src") or ""
    if not image:
        archive = major.get("archive") or {}
        if isinstance(archive, dict):
            image = archive.get("cover") or ""
    if not image:
        opus = major.get("opus") or {}
        if isinstance(opus, dict):
            pics = opus.get("pictures") or opus.get("pics") or []
            if pics and isinstance(pics[0], dict):
                image = pics[0].get("src") or pics[0].get("url") or ""

    return summary[:80], image


def _poll_once(up_uid: int, sessdata: str = "", wbi=None) -> Optional[dict]:
    """
    查询UP主最新动态

    Args:
        up_uid: UP主UID
        sessdata: 登录凭证（可选，游客也可查询）

    Returns:
        Optional[dict]: {"dynamic_id", "author", "pub_ts"}；查询失败返回None
    """
    headers = dict(HEADERS)
    if sessdata:
        headers["cookie"] = f"SESSDATA={sessdata}"
        headers["referer"] = f"https://space.bilibili.com/{up_uid}/dynamic"
    try:
        resp = requests.get(
            _build_dynamic_url(up_uid, wbi), headers=headers, timeout=10
        )
        data = resp.json()
        items = data.get("data", {}).get("items") or []
        if not items:
            return None
        first = items[0]
        modules = first.get("modules") or {}
        # 跳过置顶动态
        if (modules.get("module_tag") or {}).get("text") == "置顶":
            if len(items) < 2:
                return None
            first = items[1]
            modules = first.get("modules") or {}
        author = modules.get("module_author") or {}
        summary, image = _extract_content(modules)
        return {
            "dynamic_id": first.get("id_str", ""),
            "author": (author.get("name") or ""),
            "pub_ts": author.get("pub_ts") or 0,
            "summary": summary,
            "image": image,
        }
    except Exception as e:
        logger.warning(f"查询UP主{up_uid}动态失败: {e}")
        return None


LIVE_STATUS_API = "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
# 直播状态检查间隔（秒）；UP主在播时暂停动态轮询以降低请求量
LIVE_CHECK_INTERVAL = 600
LIVE_PAUSE_POLL = 300


def _default_state_path() -> Path:
    """
    直播状态共享文件默认路径（实例私有：数据目录下，多实例同机互不干扰）
    """
    return Path("data") / LIVE_STATE_FILENAME


def _fetch_live_states(up_uids: List[int]) -> Dict[int, dict]:
    """
    批量查询UP主直播状态（单次请求覆盖全部UID，供主进程watcher使用）

    Args:
        up_uids: UP主UID列表

    Returns:
        Dict[int, dict]: {uid: {"live": bool, "room_id": int, "title": str}}
    """
    result: Dict[int, dict] = {}
    if not up_uids:
        return result
    try:
        resp = requests.post(
            LIVE_STATUS_API, json={"uids": up_uids},
            headers={"user-agent": HEADERS["user-agent"]}, timeout=10,
        )
        data = resp.json().get("data") or {}
        for uid in up_uids:
            info = data.get(str(uid)) or {}
            result[uid] = {
                "live": info.get("live_status") == 1,
                "room_id": info.get("room_id") or 0,
                "title": info.get("title") or "",
            }
    except Exception as e:
        logger.warning(f"批量查询直播状态失败: {e}")
    return result


def _write_live_state(states: Dict[int, dict], path: Optional[Path] = None) -> None:
    """原子写入直播状态共享文件（子进程只读）"""
    try:
        path = path or _default_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(states), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"写入直播状态文件失败: {e}")


def _read_live_state(up_uid: int, path: Optional[Path] = None) -> Optional[dict]:
    """
    读取UP主直播状态（共享文件，无记录返回None）

    Args:
        up_uid: UP主UID
        path: 状态文件路径（默认数据目录）

    Returns:
        Optional[dict]: {"live", "room_id", "title"}
    """
    try:
        path = path or _default_state_path()
        if not path.exists():
            return None
        states = json.loads(path.read_text(encoding="utf-8"))
        return states.get(str(up_uid))
    except Exception:
        return None


def _dynamic_process_main(up_uid: int, push_queue, stop_event,
                          sessdata: str = "", state_path=None) -> None:
    """动态轮询子进程入口（spawn）"""
    from ..utils.logger import Logger

    Logger().setup(name="blivepost", console=True)
    last_seen = ""
    up_live = False
    logger = get_logger("dynamic")

    logger.info(f"UP主 {up_uid} 动态轮询进程已启动 (pid={os.getpid()})")
    while not stop_event.is_set():
        # 直播联动：读取主进程批量维护的共享状态文件（子进程不单独发请求），
        # UP主在播时暂停动态轮询以降低请求量
        state = _read_live_state(up_uid, path=state_path)
        new_live = bool(state and state.get("live"))
        if new_live != up_live:
            logger.info(
                f"UP主 {up_uid} {'开播，暂停动态轮询' if new_live else '下播，恢复动态轮询'}"
            )
        up_live = new_live

        if up_live:
            stop_event.wait(LIVE_PAUSE_POLL)
            continue

        info = _poll_once(up_uid, sessdata)
        if info and info["dynamic_id"] and info["dynamic_id"] != last_seen:
            is_new = bool(last_seen) and info["dynamic_id"] > last_seen
            last_seen = info["dynamic_id"]
            if is_new:
                logger.info(f"UP主 {up_uid} 发布新动态 {info['dynamic_id']}")
                try:
                    push_queue.put(
                        {"type": "dynamic", "up_uid": up_uid, **info}, timeout=5
                    )
                except Exception as e:
                    logger.error(f"动态上报写入队列失败: {e}")
        # 随机间隔轮询
        stop_event.wait(random.randint(POLL_MIN, POLL_MAX))


class DynamicManager:
    """
    动态轮询管理器（主进程）

    每个被订阅的UP主一个轮询子进程，保证每个uid仅一个进程；
    进程死亡由watcher即时重建。
    """

    def __init__(self, settings: Settings, push_queue, cookie_pool=None,
                 enabled: bool = False, state_path=None):
        """
        初始化动态管理器

        Args:
            settings: 应用配置
            push_queue: 跨进程队列（与MonitorManager共用，消费者统一处理）
            cookie_pool: Cookie账号池（轮询凭证，可选）
            enabled: 动态监控总开关（Setting.dynamic_monitor，默认关闭）
            state_path: 直播状态共享文件路径（实例私有，默认数据目录）
        """
        self.settings = settings
        self.push_queue = push_queue
        self.cookie_pool = cookie_pool
        self.enabled = enabled
        self.state_path = state_path

        self._ctx = multiprocessing.get_context("spawn")
        self._ups: Dict[int, Any] = {}
        self._stop_events: Dict[int, Any] = {}
        self._processes: Dict[int, Any] = {}
        self._watchers: Dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def ensure(self, up_uid: int) -> Tuple[bool, str]:
        """
        确保UP主动态轮询进程存在（不存在则创建，已存在则忽略）

        Args:
            up_uid: UP主UID

        Returns:
            Tuple[bool, str]: (是否新建进程, UP主名，未知时为空串)
        """
        if not self.enabled:
            logger.info("动态监控未启用（Setting.dynamic_monitor=N），忽略订阅")
            return False, ""

        uid = int(up_uid)
        async with self._lock:
            if uid in self._processes and self._processes[uid].is_alive():
                return False, ""

            sessdata = ""
            if self.cookie_pool is not None:
                account = await self.cookie_pool.acquire()
                if account is not None:
                    sessdata = account.sessdata

            stop_event = self._ctx.Event()
            proc = self._ctx.Process(
                target=_dynamic_process_main,
                args=(uid, self.push_queue, stop_event, sessdata, self.state_path),
                name=f"dynamic-{uid}",
            )
            proc.start()
            self._ups[uid] = self._ctx
            self._stop_events[uid] = stop_event
            self._processes[uid] = proc
            self._watchers[uid] = asyncio.create_task(
                self._watch(uid), name=f"watch-dynamic-{uid}"
            )
            logger.info(f"UP主 {uid} 动态轮询进程已创建 (pid={proc.pid})")
            return True, ""

    async def _watch(self, up_uid: int) -> None:
        """存活监视：进程意外退出时延迟重建"""
        proc = self._processes.get(up_uid)
        if proc is None:
            return
        await asyncio.to_thread(proc.join)
        if up_uid not in self._processes:
            return  # 已被remove
        logger.warning(f"UP主 {up_uid} 动态轮询进程意外退出，{REBUILD_DELAY}秒后重建")
        await asyncio.sleep(REBUILD_DELAY)
        if up_uid in self._processes:
            await self.remove(up_uid)
            await self.ensure(up_uid)

    async def remove(self, up_uid: int) -> bool:
        """停止并移除UP主的轮询进程"""
        uid = int(up_uid)
        async with self._lock:
            proc = self._processes.pop(uid, None)
            stop_event = self._stop_events.pop(uid, None)
            watcher = self._watchers.pop(uid, None)
            self._ups.pop(uid, None)

        if proc is None:
            return False

        if stop_event is not None:
            stop_event.set()
        await asyncio.to_thread(proc.join, 3)
        if proc.is_alive():
            proc.terminate()
            await asyncio.to_thread(proc.join, 3)
        if watcher is not None:
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):
                pass
        logger.info(f"UP主 {uid} 动态轮询进程已停止")
        return True

    def has(self, up_uid: int) -> bool:
        """UP主是否存在存活的轮询进程"""
        proc = self._processes.get(int(up_uid))
        return proc is not None and proc.is_alive()

    def up_uids(self) -> list:
        """当前轮询的UP主列表"""
        return list(self._processes.keys())

    async def stop_all(self) -> None:
        """停止全部轮询进程"""
        for uid in list(self._processes.keys()):
            await self.remove(uid)


class LiveStateWatcher:
    """
    直播状态共享watcher（主进程）

    周期性批量查询全部被订阅UP主的直播状态（单次请求覆盖全部UID），
    原子写入共享文件供轮询子进程读取；检测到开播状态变化（未播→在播）
    时经队列上报主进程，向订阅群推送开播提醒。
    """

    def __init__(self, binding_store, push_queue, interval: int = 600,
                 state_path=None):
        """
        初始化watcher

        Args:
            binding_store: 绑定存储（获取订阅的UP主列表）
            push_queue: 跨进程队列（开播提醒上报）
            interval: 检查间隔（秒）
            state_path: 状态文件路径（默认数据目录，实例私有）
        """
        self.binding_store = binding_store
        self.push_queue = push_queue
        self.interval = interval
        self.state_path = state_path
        self._last_live: Dict[int, bool] = {}
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """启动watcher（需在事件循环内调用）"""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="live-state-watcher")
            logger.info(f"直播状态watcher已启动（间隔{self.interval}秒）")

    async def stop(self) -> None:
        """停止watcher"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def run(self) -> None:
        """主循环"""
        while True:
            try:
                await self.check_once()
            except Exception as e:
                logger.error(f"直播状态检查失败: {e}")
            await asyncio.sleep(self.interval)

    async def check_once(self, up_uids: Optional[List[int]] = None) -> int:
        """
        执行一轮批量查询：写共享文件、检测开播变化并上报

        Args:
            up_uids: 指定UP主列表（默认取当前全部订阅）

        Returns:
            int: 本次检测到的开播UP主数量
        """
        if up_uids is None:
            up_uids = self.binding_store.get_dynamic_up_uids()
        if not up_uids:
            return 0

        states = await asyncio.to_thread(_fetch_live_states, up_uids)
        await asyncio.to_thread(_write_live_state, states, self.state_path)

        went_live = 0
        for uid, info in states.items():
            was_live = self._last_live.get(uid, False)
            self._last_live[uid] = info["live"]
            if info["live"] and not was_live:
                went_live += 1
                logger.info(f"UP主 {uid} 开播：{info['title']}")
                try:
                    self.push_queue.put({
                        "type": "up_live",
                        "up_uid": uid,
                        "room_id": info["room_id"],
                        "title": info["title"],
                    }, timeout=5)
                except Exception as e:
                    logger.error(f"开播提醒写入队列失败: {e}")
        return went_live
