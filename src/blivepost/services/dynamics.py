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
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

import requests

from ..config.settings import Settings
from ..utils.logger import get_logger


if TYPE_CHECKING:
    from .wbi import WbiSigner


logger = get_logger("dynamics")

DYNAMIC_API_BASE = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
LIVE_STATE_FILENAME = "blivepost_dynamic_live_state.json"
HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
}
POLL_MIN, POLL_MAX = 60, 90  # 轮询间隔随机范围（秒）
# 连续失败退避区间（风控期降频，让封锁自然冷却）
BACKOFF_MIN, BACKOFF_MAX = 300, 600
REBUILD_DELAY = 5
# 连续异常（请求失败/风控响应）告警阈值（轮）：约10轮*60-90秒≈15分钟，超过后经队列告警一次
ALERT_FAIL_ROUNDS = 10
# dynamic_seen判重表每UP保留条数上限：超出部分按动态id数值序删除更旧的，
# 防止长期订阅下表无限膨胀（id为毫秒级递增数字串，数值序即发布序）
SEEN_KEEP_LIMIT = 500
# 动态子进程日志目录默认值（未显式指定时与CLI --log-dir默认值一致）
DEFAULT_LOG_DIR = "logs"


def _build_dynamic_url(up_uid: int, wbi=None) -> str:
    """
    构造动态feed接口URL（带WBI签名；签名不可用时降级为无签名URL）

    Args:
        up_uid: UP主UID
        wbi: WBI签名器（WbiSigner实例或可复用的_WbiSignerCache；
            None时尝试新建，轮询循环应传入复用实例避免每轮nav取钥）

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


class _WbiSignerCache:
    """
    轮询循环内复用的WBI签名器

    每轮新建WbiSigner会导致每UP每60-90秒多发一次nav请求取WBI密钥
    （nav高频无登录态访问是风控高发点）。WBI密钥约1天有效，这里在
    轮询循环内复用同一签名器实例，按小时级TTL惰性重建，签名失败
    （nav取钥失败等）时丢弃实例待下一轮重建。

    实现与WbiSigner相同的sign_url_params鸭子类型接口，可直接作为
    wbi参数传入_poll_once/_build_dynamic_url。
    """

    # 外层TTL取12小时（略短于WbiSigner内部到次日零点的密钥缓存）：
    # 过短（如1小时）会让nav取钥回升到~24次/天/UP，违背降低nav频率的初衷；
    # 密钥真正失效时签名失败会触发invalidate即时重建，无需依赖短TTL兜底
    REFRESH_SECONDS = 12 * 3600

    def __init__(self, now: Optional[Callable[[], float]] = None):
        """
        Args:
            now: 时钟函数（可注入，测试用）；默认time.monotonic——TTL判定
                度量单调流逝时间，不受系统墙钟跳变（NTP校时/休眠唤醒）影响
        """
        self._signer: Optional["WbiSigner"] = None
        self._built_at: float = 0.0
        self._now: Callable[[], float] = now or time.monotonic

    def _build(self) -> "WbiSigner":
        from .wbi import WbiSigner
        self._signer = WbiSigner()
        self._built_at = self._now()
        return self._signer

    def acquire(self) -> "WbiSigner":
        """获取可复用的签名器实例（未创建或超TTL时重建）"""
        if self._signer is None or self._now() - self._built_at >= self.REFRESH_SECONDS:
            return self._build()
        return self._signer

    def invalidate(self) -> None:
        """签名失败时丢弃实例（下一轮自动重建）"""
        self._signer = None

    def sign_url_params(self, params: str) -> Tuple[str, str]:
        """签名URL参数；签名失败（空w_rid）时丢弃实例，降级由调用方处理"""
        w_rid, wts = self.acquire().sign_url_params(params)
        if not w_rid:
            self.invalidate()
        return w_rid, wts


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


class _RemoteFetchProxy:
    """主进程全局单浏览器取数服务的子进程侧代理。

    背景（2026-11-10真机实证）：feed接口对python requests（含curl-cffi模拟
    TLS指纹）一律请求级风控412，仅真实浏览器导航正常——动态取数依赖无头
    浏览器。为控制内存（每实例~300MB），**全局仅主进程持有一个浏览器**
    （DynFetchService），不论订阅多少UP；本代理经本机HTTP调用该服务。
    服务不可用/失败时由调用方回退requests链（_poll_once），不影响尽力取数。
    """

    def __init__(self, port: int, token: str):
        self._port = int(port)
        self._token = token

    def fetch_json(self, url: str) -> Optional[dict]:
        """经主进程浏览器取数；任何失败返回None（调用方走requests兜底）"""
        try:
            resp = requests.post(
                f"http://127.0.0.1:{self._port}/dyn-fetch",
                json={"url": url},
                headers={"X-Fetch-Token": self._token},
                timeout=40,
            )
            if resp.status_code != 200:
                logger.warning(f"浏览器取数服务响应异常: HTTP {resp.status_code}")
                return None
            data = (resp.json() or {}).get("data")
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.warning(f"浏览器取数服务不可达: {e}")
            return None


def _poll_once(up_uid: int, cookie_string: str = "", wbi=None) -> Optional[List[dict]]:
    """
    查询UP主动态列表（置顶跳过，返回全部可见条目）

    Args:
        up_uid: UP主UID
        cookie_string: 完整cookie（含buvid3等风控必需项；仅SESSDATA会412，
            2026-11-10真机实证；空串=游客）
        wbi: WBI签名器（轮询循环应传入_WbiSignerCache复用实例）

    Returns:
        Optional[List[dict]]: 按接口顺序（新到旧）的动态信息列表，每项含
        {"dynamic_id", "author", "pub_ts", "summary", "image"}；请求异常或
        接口风控/错误响应（code!=0，如-352/-412）返回None——与正常空列表
        区分，供连续异常计数告警（_DynamicPollSession）。

    风控自愈（2026-11-10真机实证）：带WBI签名的请求412（平台轮换mixin
    表致旧签名反被判风控）而无签名+完整cookie正常——签名请求失败时自动
    回退无签名重试一次；若未来平台强制签名，无签名失败亦计异常轮。
    """
    headers = dict(HEADERS)
    headers["referer"] = f"https://space.bilibili.com/{up_uid}/dynamic"
    if cookie_string:
        headers["cookie"] = cookie_string

    def _fetch(url: str) -> Optional[dict]:
        """单次请求：返回解析后的data dict；风控/异常返回None"""
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            data = resp.json()
            if data.get("code") != 0:
                logger.warning(f"查询UP主{up_uid}动态接口返回异常码 {data.get('code')}")
                return None
            return data
        except Exception as e:
            logger.warning(f"查询UP主{up_uid}动态请求失败(HTTP层): {e}")
            return None

    signed_url = _build_dynamic_url(up_uid, wbi)
    data = _fetch(signed_url)
    if data is None and "w_rid=" in signed_url:
        # 签名请求被风控（412/空响应/异常码）→ 去签名重试
        unsigned_url = signed_url.split("&wts=")[0]
        logger.info(f"UP主{up_uid} 签名请求被风控，回退无签名请求")
        data = _fetch(unsigned_url)
    if data is None:
        return None
    return _parse_feed_data(data, up_uid)


def _parse_feed_data(data: dict, up_uid: int) -> Optional[List[dict]]:
    """解析feed接口响应（浏览器/requests两路共用）；解析异常返回None"""
    try:
        items = (data.get("data") or {}).get("items") or []
        infos: List[dict] = []
        for item in items:
            modules = item.get("modules") or {}
            # 跳过置顶动态。已知行为：发布即置顶的动态在置顶期间被跳过、
            # 不记入已见；取消置顶后它重新出现在feed中，届时作为"新动态"
            # 补推（延迟推送）。置顶动态多为长期广告/公告，实时推送反而
            # 刷屏，此权衡可接受，不改逻辑
            if (modules.get("module_tag") or {}).get("text") == "置顶":
                continue
            dynamic_id = item.get("id_str") or ""
            if not dynamic_id:
                continue
            author = modules.get("module_author") or {}
            summary, image = _extract_content(modules)
            infos.append({
                "dynamic_id": dynamic_id,
                "author": (author.get("name") or ""),
                "pub_ts": author.get("pub_ts") or 0,
                "summary": summary,
                "image": image,
            })
        return infos
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


def _dynamics_dir(dynamics_dir=None) -> Path:
    """已见动态子库目录（默认数据目录下dynamics/，实例可指定）"""
    return Path(dynamics_dir) if dynamics_dir else Path("data") / "dynamics"


def _seen_db_path(up_uid: int, dynamics_dir=None) -> Path:
    return _dynamics_dir(dynamics_dir) / f"up_{up_uid}.db"


def _init_seen_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS dynamic_seen ("
            "dynamic_id TEXT PRIMARY KEY, up_uid INTEGER, "
            "summary TEXT DEFAULT '', created_at TEXT)"
        )
        conn.commit()
    finally:
        conn.close()


def load_seen_dynamic_ids(up_uid: int, dynamics_dir=None) -> set:
    """加载该UP主已推送/已记录的动态ID集合（持久于每UP独立子库）"""
    path = _seen_db_path(up_uid, dynamics_dir)
    if not path.exists():
        return set()
    try:
        _init_seen_db(path)
        conn = sqlite3.connect(path, timeout=5)
        try:
            return {row[0] for row in conn.execute("SELECT dynamic_id FROM dynamic_seen")}
        finally:
            conn.close()
    except Exception:
        return set()


def save_seen_dynamic_ids(up_uid: int, ids, dynamics_dir=None) -> None:
    """记录动态ID（INSERT OR IGNORE，防止发布后删除的旧动态再次触发）；
    记录后修剪判重表，按动态id数值序仅保留最新SEEN_KEEP_LIMIT条（防膨胀）"""
    path = _seen_db_path(up_uid, dynamics_dir)
    try:
        _init_seen_db(path)
        conn = sqlite3.connect(path, timeout=5)
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO dynamic_seen(dynamic_id, up_uid, created_at) "
                "VALUES(?, ?, ?)",
                [(i, up_uid, datetime.now().isoformat(timespec="seconds")) for i in ids],
            )
            # 修剪：按dynamic_id数值序保留最新SEEN_KEEP_LIMIT条，更旧的删除。
            # 不做COUNT前置判断——未超限时DELETE无行可删，代价可忽略
            conn.execute(
                "DELETE FROM dynamic_seen WHERE rowid NOT IN ("
                "SELECT rowid FROM dynamic_seen "
                "ORDER BY CAST(dynamic_id AS INTEGER) DESC LIMIT ?)",
                (SEEN_KEEP_LIMIT,),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        # 写失败仅影响判重（最坏情况：下轮重复推一次），记日志便于排查，
        # 不中断轮询
        logger.error(f"记录已见动态失败(UP主{up_uid}): {e}")


class _DynamicPollSession:
    """
    单UP主轮询会话状态（动态子进程内使用）

    - 漏推修复：一轮取列表中全部未见过的动态（已见ID持久化判重），
      按发布时间从旧到新逐条经队列上报——UP短时间连发多条时中间
      动态不再漏推；
    - 基线轮：首轮仅记录不推送（空库首启/升级防刷屏；baseline_first
      供重新订阅场景强制基线化，防止停订期间的新动态被一次性补推）；
    - 风控告警：连续异常（请求失败/风控响应）超过阈值（ALERT_FAIL_ROUNDS轮）
      时经队列告警一次；恢复正常响应（含空列表）后重置计数（可再次告警），
      告警后持续异常期间不重复告警。
    """

    def __init__(self, up_uid: int, seen: set, push_queue, dynamics_dir=None,
                 alert_rounds: int = ALERT_FAIL_ROUNDS,
                 baseline_first: bool = False):
        """
        Args:
            up_uid: UP主UID
            seen: 已见动态ID集合（load_seen_dynamic_ids加载，含持久化判重）
            push_queue: 跨进程队列（新动态/告警上报主进程）
            dynamics_dir: 已见动态子库目录
            alert_rounds: 连续异常告警阈值（轮）
            baseline_first: True时首轮无条件基线化（seen非空也把当前可见动态
                全部记为已见、不推送）——用于取消订阅后重新订阅，防止停订
                期间的新动态被一次性补推；False（默认）按seen表是否为空判定
                （进程重启/开关恢复等运行中恢复沿用已见记录，不因恢复漏推）
        """
        self.up_uid = up_uid
        self.seen = seen
        self.push_queue = push_queue
        self.dynamics_dir = dynamics_dir
        self.alert_rounds = alert_rounds
        # 基线轮：首轮仅记录不推送（防升级/首启/重订阅刷屏）。默认seen表
        # 非空即视为已基线化；baseline_first=True则强制基线化一轮
        self.baseline = True if baseline_first else not seen
        self.fail_streak = 0
        self.alerted = False

    def handle(self, infos: Optional[List[dict]]) -> None:
        """
        处理一轮轮询结果

        Args:
            infos: _poll_once返回的动态列表（None=请求异常/风控响应）
        """
        if infos is None:
            # 仅异常（请求失败/接口风控响应）计连续失败；正常空列表
            # （UP删光动态/当前全部置顶）是有效响应，与有数据一同走
            # 下方重置逻辑，不参与告警计数
            self._count_failure()
            return
        # 恢复正常响应（含空列表）：重置连续计数与告警去重标记
        self.fail_streak = 0
        self.alerted = False
        fresh = [i for i in infos
                 if i.get("dynamic_id") and i["dynamic_id"] not in self.seen]
        if not fresh:
            return
        self.seen.update(i["dynamic_id"] for i in fresh)
        save_seen_dynamic_ids(
            self.up_uid, [i["dynamic_id"] for i in fresh], self.dynamics_dir
        )
        if self.baseline:
            self.baseline = False  # 首批仅作基线，不推送
            return
        # feed按新到旧返回：按发布时间从旧到新逐条上报，连发多条不漏推
        for info in sorted(fresh, key=lambda x: x.get("pub_ts") or 0):
            logger.info(f"UP主 {self.up_uid} 发布新动态 {info['dynamic_id']}")
            self._put({"type": "dynamic", "up_uid": self.up_uid, **info})

    def _count_failure(self) -> None:
        """连续异常（请求失败/风控响应）计数，超过阈值经队列告警一次（去重：不重复告警）"""
        self.fail_streak += 1
        if self.fail_streak < self.alert_rounds or self.alerted:
            return
        self.alerted = True
        logger.error(
            f"UP主 {self.up_uid} 动态接口连续{self.fail_streak}轮异常"
            f"（请求失败/风控），经队列上报管理员告警"
        )
        self._put({
            "type": "dynamic_alert",
            "up_uid": self.up_uid,
            "consecutive": self.fail_streak,
        })

    def _put(self, item: dict) -> None:
        try:
            self.push_queue.put(item, timeout=5)
        except Exception as e:
            logger.error(f"动态上报写入队列失败: {e}")


def _dynamic_process_main(up_uid: int, push_queue, stop_event,
                          cookie_string: str = "", state_path=None,
                          dynamics_dir=None, log_dir=None,
                          baseline_first: bool = False,
                          fetch_port: int = 0, fetch_token: str = "") -> None:
    """动态轮询子进程入口（spawn）；baseline_first见_DynamicPollSession

    cookie_string传完整cookie（buvid3等风控必需；仅SESSDATA会412），
    供requests兜底链使用；浏览器取数走主进程全局服务（fetch_port/token）。
    """
    from ..utils.logger import Logger

    # 独立日志文件（blivepost_dyn_<uid>.<日期>.log）：与父进程及房间子进程
    # 分离，避免多进程同写一文件在Windows轮转时PermissionError丢日志
    Logger().setup(name="blivepost", log_dir=log_dir or DEFAULT_LOG_DIR,
                   console=True, log_suffix=f"dyn_{up_uid}")
    # 已见动态列表持久于每UP独立sqlite子库：按ID判重，发布后删除的
    # 旧动态/进程重启都不会再触发推送（替代旧last_seen比较逻辑）
    seen = load_seen_dynamic_ids(up_uid, dynamics_dir)
    session = _DynamicPollSession(up_uid, seen, push_queue, dynamics_dir,
                                  baseline_first=baseline_first)
    # WBI签名器在轮询循环内复用（小时级TTL惰性重建+失败重建），避免每轮nav取钥
    signer = _WbiSignerCache()
    # 全局单浏览器取数代理（主进程DynFetchService）：真实浏览器环境过
    # 请求级风控——requests/curl-cffi一律412而浏览器导航正常（真机实证）
    fetch_proxy = _RemoteFetchProxy(fetch_port, fetch_token) if fetch_port else None
    up_live = False
    served_logged = False
    logger = get_logger("dynamic")

    logger.info(f"UP主 {up_uid} 动态轮询进程已启动 (pid={os.getpid()})")
    # 父进程存活自检：主进程被强杀（kill -9/taskkill）时stop_event不会置位，
    # 子进程会泄漏续跑（2026-11-10真机累积11个泄漏进程）——每轮检查父进程
    parent_proc = multiprocessing.parent_process()
    while not stop_event.is_set():
        if parent_proc is not None and not parent_proc.is_alive():
            logger.warning("父进程已退出，动态轮询子进程自动退出（防泄漏）")
            return
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

        # 取数三级链：主进程浏览器 → requests签名 → requests无签名
        infos = None
        data = None
        if fetch_proxy is not None:
            data = fetch_proxy.fetch_json(
                f"{DYNAMIC_API_BASE}?host_mid={up_uid}&platform=web")
            if data is not None:
                infos = _parse_feed_data(data, up_uid)
                if not served_logged:
                    served_logged = True
                    logger.info(f"UP主 {up_uid} 浏览器取数服务链路正常")
        if data is None:
            infos = _poll_once(up_uid, cookie_string, signer)
        session.handle(infos)
        # 轮询间隔：正常60-90秒；连续失败≥3轮进入退避（5-10分钟）——
        # 风控期高频重试只会延长封锁（2026-11-10真机教训），成功后恢复
        if session.fail_streak >= 3:
            stop_event.wait(random.randint(BACKOFF_MIN, BACKOFF_MAX))
        else:
            stop_event.wait(random.randint(POLL_MIN, POLL_MAX))


class DynamicManager:
    """
    动态轮询管理器（主进程）

    每个被订阅的UP主一个轮询子进程，保证每个uid仅一个进程；
    进程死亡由watcher即时重建。
    """

    def __init__(self, settings: Settings, push_queue, cookie_pool=None,
                 enabled: bool = False, state_path=None, dynamics_dir=None,
                 log_dir=None, fetch_service=None):
        """
        初始化动态管理器

        Args:
            settings: 应用配置
            push_queue: 跨进程队列（与MonitorManager共用，消费者统一处理）
            cookie_pool: Cookie账号池（轮询凭证，可选）
            enabled: 动态监控总开关（Setting.dynamic_monitor，默认关闭）
            state_path: 直播状态共享文件路径（实例私有，默认数据目录）
            log_dir: 子进程日志目录（None时子进程用默认logs/，独立文件名）
            fetch_service: 全局单浏览器取数服务（DynFetchService，可选）——
                提供时子进程优先经其取数（过请求级风控），否则仅requests链
        """
        self.settings = settings
        self.push_queue = push_queue
        self.cookie_pool = cookie_pool
        self.enabled = enabled
        self.state_path = state_path
        self.dynamics_dir = dynamics_dir
        self.log_dir = log_dir
        self.fetch_service = fetch_service

        self._ctx = multiprocessing.get_context("spawn")
        self._ups: Dict[int, Any] = {}
        self._stop_events: Dict[int, Any] = {}
        self._processes: Dict[int, Any] = {}
        self._watchers: Dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def ensure(self, up_uid: int, baseline_first: bool = False) -> Tuple[bool, str]:
        """
        确保UP主动态轮询进程存在（不存在则创建，已存在则忽略）

        Args:
            up_uid: UP主UID
            baseline_first: 新建进程首轮是否无条件基线化（当前可见动态全部
                记为已见、不推送）。用户主动订阅（命令/面板）传True——防止
                取消订阅后重新订阅时，停订期间的新动态被一次性补推；
                False（默认）沿用seen表判重——进程重启/崩溃重建/开关恢复等
                运行中恢复场景不漏推。进程已存活时本参数无效果

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

            cookie_string = ""
            if self.cookie_pool is not None:
                account = await self.cookie_pool.acquire()
                if account is not None:
                    # 完整cookie（含buvid3等风控必需项；仅SESSDATA实测412）
                    cookie_string = account.cookie_string or ""
                    # 全局浏览器服务的cookie同步（各UP共用单浏览器实例）
                    if self.fetch_service is not None:
                        self.fetch_service.set_cookies(cookie_string)

            fetch_port = self.fetch_service.port if self.fetch_service else 0
            fetch_token = self.fetch_service.token if self.fetch_service else ""
            stop_event = self._ctx.Event()
            proc = self._ctx.Process(
                target=_dynamic_process_main,
                args=(uid, self.push_queue, stop_event, cookie_string, self.state_path,
                      self.dynamics_dir, self.log_dir, baseline_first,
                      fetch_port, fetch_token),
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
            # 运行中恢复（崩溃重建）：baseline_first保持默认False，沿用seen表判重
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
