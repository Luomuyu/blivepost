# -*- coding: utf-8 -*-
"""动态轮询遗留修复回归测试：

- 1a WbiSigner复用：轮询循环内复用签名器（TTL惰性重建+失败重建），
     不再每轮新建（每轮多发nav请求）；TTL判定用注入时钟，不依赖真实墙钟
- 1b 候选检测（2026-11-10改）：feed只取"置顶+最新非置顶"中id更大者为候选，
     内容经opus页渲染（prepare回调）获取，无回调回退feed摘要
- 1c 风控/异常告警：连续异常（请求失败/风控）超阈值经队列告警一次（恢复
     重置、不重复）；正常空列表是有效响应，不计失败
- 1d 重新订阅基线化：baseline_first=True时首轮无条件记seen不补推
     （防止停订期间的新动态被一次性补推）；seen修剪防膨胀；置顶动态
     更新时立即推送（不再跳过/延迟）
- 2  子进程日志分离：setup产生的日志文件名含进程标识（room_<id>/dyn_<uid>）
"""

import asyncio
import logging
import queue

from blivepost.config.settings import Settings
from blivepost.utils.logger import DailyRotatingFileHandler, Logger


class FakeQueue:
    def __init__(self):
        self.items = []

    def put(self, item, timeout=None):
        self.items.append(item)


def _feed_response(items, code=0):
    class FakeResp:
        def json(self):
            return {"code": code, "data": {"items": items}}

    return FakeResp()


def _item(did, name="UP甲", pub_ts=1700000000, text="内容"):
    return {
        "id_str": str(did),
        "modules": {
            "module_author": {"name": name, "pub_ts": pub_ts},
            "module_dynamic": {"desc": {"text": text}, "major": {}},
        },
    }


# ==================== 1a WbiSigner复用 ====================


class CountingSigner:
    """计数桩：记录实例构建次数与nav取钥次数（密钥缓存语义同真实WbiSigner）"""

    built = 0
    nav_calls = 0

    def __init__(self):
        CountingSigner.built += 1
        self._key = None

    def get_w_key(self, force_refresh=False):
        if self._key is None or force_refresh:
            CountingSigner.nav_calls += 1  # nav请求仅在密钥缺失/过期时发生
            self._key = "k" * 32
        return self._key

    def sign_url_params(self, params):
        wts = "1700000000"
        from blivepost.services.wbi import wbi_sign
        return wbi_sign(params, self.get_w_key(), wts), wts


class FailingSigner(CountingSigner):
    """签名失败桩（模拟nav取钥失败，w_rid为空）"""

    def sign_url_params(self, params):
        self.get_w_key()
        return "", "1700000000"


class FakeClock:
    """注入时钟（确定性TTL测试）：显式advance步进，不依赖真实墙钟
    （全量跑时系统NTP校时/休眠唤醒不再影响TTL判定）"""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_wbi_signer_cache_reuses_instance_within_ttl(monkeypatch):
    """TTL内多轮签名只构建一个签名器、只发一次nav取钥请求
    （注入时钟模拟每轮轮询间隔，累计远小于TTL，判定不依赖真实墙钟）"""
    from blivepost.services import dynamics as dyn
    from blivepost.services import wbi as wbi_mod

    CountingSigner.built = 0
    CountingSigner.nav_calls = 0
    monkeypatch.setattr(wbi_mod, "WbiSigner", CountingSigner)

    clock = FakeClock()
    cache = dyn._WbiSignerCache(now=clock)
    for _ in range(5):  # 模拟5轮轮询（每轮间隔取上限90秒）
        clock.advance(90)
        w_rid, _ = cache.sign_url_params("host_mid=1&platform=web")
        assert len(w_rid) == 32

    assert CountingSigner.built == 1     # 不重复构建
    assert CountingSigner.nav_calls == 1  # nav取钥仅一次


def test_wbi_signer_cache_rebuilds_after_ttl(monkeypatch):
    """超过TTL后惰性重建签名器实例（注入时钟确定性越过TTL边界）"""
    from blivepost.services import dynamics as dyn
    from blivepost.services import wbi as wbi_mod

    CountingSigner.built = 0
    monkeypatch.setattr(wbi_mod, "WbiSigner", CountingSigner)

    clock = FakeClock()
    cache = dyn._WbiSignerCache(now=clock)
    cache.sign_url_params("p=1")
    clock.advance(dyn._WbiSignerCache.REFRESH_SECONDS + 1)  # 确定性越过TTL
    cache.sign_url_params("p=1")
    assert CountingSigner.built == 2


def test_wbi_signer_cache_rebuilds_on_sign_failure(monkeypatch):
    """签名失败（nav取钥失败）时丢弃实例，下一轮自动重建"""
    from blivepost.services import dynamics as dyn
    from blivepost.services import wbi as wbi_mod

    CountingSigner.built = 0
    monkeypatch.setattr(wbi_mod, "WbiSigner", FailingSigner)

    cache = dyn._WbiSignerCache()
    w_rid, _ = cache.sign_url_params("p=1")
    assert w_rid == ""  # 失败降级由调用方处理
    assert cache._signer is None  # 实例已丢弃
    cache.sign_url_params("p=1")
    assert CountingSigner.built == 2  # 失败后重建


def test_requests_fetch_reuses_shared_signer_across_rounds(monkeypatch):
    """_requests_fetch多轮共用同一签名器缓存：WbiSigner仅构建一次"""
    from blivepost.services import dynamics as dyn
    from blivepost.services import wbi as wbi_mod

    CountingSigner.built = 0
    CountingSigner.nav_calls = 0
    monkeypatch.setattr(wbi_mod, "WbiSigner", CountingSigner)
    monkeypatch.setattr(
        dyn.requests, "get",
        lambda *a, **kw: _feed_response([_item("900")]),
    )

    cache = dyn._WbiSignerCache()
    assert dyn._requests_fetch(111, "", cache) is not None
    assert dyn._requests_fetch(111, "", cache) is not None
    assert CountingSigner.built == 1  # 两轮轮询未重复构建签名器


# ==================== 1b 漏推中间动态 ====================


def test_extract_candidate_id_pinned_vs_latest(monkeypatch):
    """候选检测：置顶与最新非置顶取dynamic_id更大者（不跳过置顶）"""
    import blivepost.services.dynamics as dyn

    def feed(items):
        return {"code": 0, "data": {"items": items}}

    def item(dyn_id, pinned=False):
        m = {"module_author": {"name": "UP"}}
        if pinned:
            m["module_tag"] = {"text": "置顶"}
        return {"id_str": str(dyn_id), "modules": m}

    # 有置顶且置顶更新（刚发布即置顶）→ 推置顶那条
    assert dyn._extract_candidate_id(
        feed([item(300, pinned=True), item(200)])) == "300"
    # 有置顶但最新非置顶更新 → 推最新
    assert dyn._extract_candidate_id(
        feed([item(100, pinned=True), item(400)])) == "400"
    # 无置顶 → items[0]即最新
    assert dyn._extract_candidate_id(feed([item(500), item(100)])) == "500"
    # 空feed → None
    assert dyn._extract_candidate_id(feed([])) is None
    # module_tag为null（非dict）→ 不视作置顶
    assert dyn._extract_candidate_id(
        feed([{"id_str": "600", "modules": {"module_tag": None,
                                            "module_author": {}}}])) == "600"


def test_poll_session_pushes_new_candidate_with_prepare(tmp_path):
    """新候选动态：经prepare（opus渲染）构造内容并上报，feed只供ID"""
    from blivepost.services.dynamics import (
        _DynamicPollSession, load_seen_dynamic_ids,
    )

    q = queue.Queue()
    seen = load_seen_dynamic_ids(111, str(tmp_path))
    session = _DynamicPollSession(111, seen, q, dynamics_dir=str(tmp_path))
    # 首轮基线：记录候选不推送
    session.handle({"data": {"items": [{"id_str": "100", "modules": {
        "module_tag": None, "module_author": {}}}]}}, prepare=lambda i: None)
    assert q.empty() and "100" in session.seen
    # 新候选：prepare返回opus渲染内容（含图）
    session.handle({"data": {"items": [{"id_str": "200", "modules": {
        "module_author": {}}}]}},
        prepare=lambda i: {"author": "UP", "summary": "新动态标题",
                           "image": "http://img/1.png", "image_source": "card"})
    item = q.get_nowait()
    assert item["type"] == "dynamic" and item["dynamic_id"] == "200"
    assert item["summary"] == "新动态标题" and item["image"] == "http://img/1.png"
    assert item["image_source"] == "card"


def test_poll_session_baseline_records_candidate_only(tmp_path):
    """基线轮仅记录候选ID不推送；此后同候选不再触发"""
    from blivepost.services.dynamics import (
        _DynamicPollSession, load_seen_dynamic_ids,
    )

    q = queue.Queue()
    seen = load_seen_dynamic_ids(111, str(tmp_path))
    session = _DynamicPollSession(111, seen, q, dynamics_dir=str(tmp_path))
    feed = {"data": {"items": [{"id_str": "100", "modules": {
        "module_author": {}}}]}}
    session.handle(feed)                       # 基线轮
    def _no_prepare(i):
        raise AssertionError("已见候选不应触发prepare")
    session.handle(feed, prepare=_no_prepare)  # 已见，不触发prepare
    assert q.empty()
    assert load_seen_dynamic_ids(111, str(tmp_path)) == {"100"}


# ==================== 1c 风控/异常告警 ====================


def test_requests_fetch_risk_control_code_returns_none(monkeypatch):
    """风控/错误响应码返回None，正常空数据返回空列表（两者区分）"""
    from blivepost.services import dynamics as dyn

    monkeypatch.setattr(dyn.requests, "get",
                        lambda *a, **kw: _feed_response([], code=-352))
    assert dyn._requests_fetch(111) is None  # 风控响应

    monkeypatch.setattr(dyn.requests, "get",
                        lambda *a, **kw: _feed_response([], code=0))
    assert dyn._requests_fetch(111) == {"code": 0, "data": {"items": []}}  # 有效空


def test_consecutive_failures_alert_once_and_reset_on_recovery(tmp_path):
    """连续异常超过阈值经队列告警一次；恢复后重置，再失败可再次告警"""
    from blivepost.services.dynamics import _DynamicPollSession

    q = FakeQueue()
    session = _DynamicPollSession(111, {"800"}, q, dynamics_dir=str(tmp_path),
                                  alert_rounds=3)

    for _ in range(5):  # 连续5轮异常（阈值3）
        session.handle(None)
    alerts = [i for i in q.items if i["type"] == "dynamic_alert"]
    assert len(alerts) == 1  # 告警后不重复
    assert alerts[0]["up_uid"] == 111 and alerts[0]["consecutive"] == 3

    # 恢复正常：重置计数（feed原始dict）
    session.handle({"code": 0, "data": {"items": [
        {"id_str": "901", "modules": {"module_author": {}}}]}})
    assert session.fail_streak == 0

    # 恢复后再次连续异常：可再次告警（一次）
    for _ in range(4):
        session.handle(None)
    alerts = [i for i in q.items if i["type"] == "dynamic_alert"]
    assert len(alerts) == 2


def test_empty_response_is_normal_not_failure(tmp_path):
    """正常空响应（空列表）不计失败：重置连续计数与告警去重，不触发告警
    （与异常None区分：仅后者计入连续失败）"""
    from blivepost.services.dynamics import _DynamicPollSession

    q = FakeQueue()
    session = _DynamicPollSession(111, {"800"}, q, dynamics_dir=str(tmp_path),
                                  alert_rounds=3)
    session.handle(None)
    session.handle(None)
    assert session.fail_streak == 2

    session.handle({"code": 0, "data": {"items": []}})  # 空feed=有效响应：重置
    assert session.fail_streak == 0

    for _ in range(5):  # 持续空feed（UP删光动态等）不会触发告警
        session.handle({"code": 0, "data": {"items": []}})
    assert session.fail_streak == 0
    assert all(i["type"] != "dynamic_alert" for i in q.items)


def test_failure_streak_resets_below_threshold(tmp_path):
    """未超阈值的间断异常不触发告警"""
    from blivepost.services.dynamics import _DynamicPollSession

    q = FakeQueue()
    session = _DynamicPollSession(111, {"800"}, q, dynamics_dir=str(tmp_path),
                                  alert_rounds=3)
    session.handle(None)
    session.handle(None)
    session.handle({"code": 0, "data": {"items": [
        {"id_str": "901", "modules": {"module_author": {}}}]}})
    session.handle(None)
    assert session.fail_streak == 1
    assert all(i["type"] != "dynamic_alert" for i in q.items)


# ==================== 1d 重新订阅基线化 / seen修剪 / 置顶延迟推 ====================


def test_resubscribe_baseline_first_suppresses_catchup_push(tmp_path):
    """重新订阅（seen非空+baseline_first=True）：首轮无条件基线化，
    停订期间的新动态记为seen不补推；其后新候选正常推送"""
    from blivepost.services.dynamics import (
        _DynamicPollSession, load_seen_dynamic_ids,
    )

    q = FakeQueue()
    seen = {"800"}  # 停订前已见（seen表非空）
    session = _DynamicPollSession(111, seen, q, dynamics_dir=str(tmp_path),
                                  baseline_first=True)

    # 重订阅首轮：停订期间发布的动态可见（候选=最新901）→ 基线化不补推
    session.handle({"data": {"items": [
        {"id_str": "901", "modules": {"module_author": {}}},
        {"id_str": "900", "modules": {"module_author": {}}},
    ]}})
    assert q.items == []  # 基线化：不补推
    assert "901" in load_seen_dynamic_ids(111, str(tmp_path))


def test_dynamic_process_main_passes_baseline_first(monkeypatch, tmp_path):
    """子进程入口把baseline_first透传给轮询会话（True/默认False两档）"""
    from blivepost.services import dynamics as dyn
    from blivepost.utils.logger import Logger

    recorded = {}

    class StubSession:
        def __init__(self, up_uid, seen, push_queue, dynamics_dir=None,
                     alert_rounds=None, baseline_first=False):
            recorded["baseline_first"] = baseline_first

        def handle(self, infos):
            pass

    monkeypatch.setattr(
        Logger, "setup",
        lambda self, *a, **kw: logging.getLogger("blivepost"))
    monkeypatch.setattr(dyn, "load_seen_dynamic_ids", lambda uid, d=None: set())
    monkeypatch.setattr(dyn, "_DynamicPollSession", StubSession)

    class StoppedEvent:
        def is_set(self):
            return True  # 轮询主循环立即退出

    dyn._dynamic_process_main(111, FakeQueue(), StoppedEvent(),
                              dynamics_dir=str(tmp_path), baseline_first=True)
    assert recorded["baseline_first"] is True

    dyn._dynamic_process_main(111, FakeQueue(), StoppedEvent(),
                              dynamics_dir=str(tmp_path))
    assert recorded["baseline_first"] is False


async def test_ensure_passes_baseline_first_to_process_args(monkeypatch):
    """ensure把baseline_first作为子进程入口末位参数透传（True/默认False）"""
    from blivepost.services import dynamics as dyn

    recorded = {}

    class FakeProc:
        pid = 4321

        def is_alive(self):
            return True

        def start(self):
            pass

        def join(self, timeout=None):
            return None

        def terminate(self):
            pass

    class FakeEvent:
        def set(self):
            pass

        def is_set(self):
            return True

    class FakeCtx:
        def Event(self):
            return FakeEvent()

        def Process(self, target=None, args=(), name=None):
            recorded["target"] = target
            recorded["args"] = args
            return FakeProc()

    async def fake_watch(self, uid):
        return None

    monkeypatch.setattr(dyn.DynamicManager, "_watch", fake_watch)
    m = dyn.DynamicManager(Settings(), push_queue=None, enabled=True)
    m._ctx = FakeCtx()

    await m.ensure(111, baseline_first=True)
    assert recorded["target"] is dyn._dynamic_process_main
    assert recorded["args"][0] == 111
    assert recorded["args"][-3] is True          # baseline_first（其后是fetch_port/token）
    assert recorded["args"][-2:] == (0, "")     # 无取数服务：port=0/token空

    await m.ensure(222)  # 默认False：运行中恢复（重启/开关恢复/崩溃重建）语义
    assert recorded["args"][0] == 222
    assert recorded["args"][-3] is False

    # 配置取数服务后透传端口/token
    class FakeSvc:
        port = 18080
        token = "TK"

        def set_cookies(self, cookie_string):
            pass

    m2 = dyn.DynamicManager(Settings(), push_queue=None, enabled=True,
                            fetch_service=FakeSvc())
    m2._ctx = FakeCtx()
    await m2.ensure(333, baseline_first=True)
    assert recorded["args"][-2:] == (18080, "TK")

    await asyncio.sleep(0)  # 让桩watcher任务运行完毕，避免task销毁告警


def test_save_seen_prunes_beyond_keep_limit(tmp_path):
    """dynamic_seen防膨胀：保存后按动态id数值序仅保留最新500条，更旧的删除"""
    from blivepost.services.dynamics import (
        SEEN_KEEP_LIMIT, load_seen_dynamic_ids, save_seen_dynamic_ids,
    )

    ids = [str(7_000_000_000_000_000_000 + i) for i in range(600)]
    ids.append("999")  # 更早期的短id：数值序最先淘汰
    save_seen_dynamic_ids(111, ids, str(tmp_path))

    kept = load_seen_dynamic_ids(111, str(tmp_path))
    assert len(kept) == SEEN_KEEP_LIMIT
    assert "999" not in kept
    assert kept == set(sorted(ids[:-1], key=int)[-500:])  # 保留数值最大500条


def test_save_seen_write_failure_logged(monkeypatch, tmp_path):
    """写库失败不再静默：记error日志且不抛出（轮询不中断）"""
    from blivepost.services import dynamics as dyn
    from blivepost.services.dynamics import save_seen_dynamic_ids

    errors = []
    monkeypatch.setattr(dyn.logger, "error", lambda msg, *a: errors.append(msg))

    def boom(*a, **kw):
        raise RuntimeError("db locked")

    monkeypatch.setattr(dyn.sqlite3, "connect", boom)
    save_seen_dynamic_ids(111, ["900"], str(tmp_path))  # 不抛出
    assert len(errors) == 1
    assert "111" in errors[0] and "已见动态" in errors[0]


def test_pinned_dynamic_pushed_immediately_when_newer(tmp_path):
    """置顶比最新非置顶更新时立即推送（不再等取消置顶）"""
    from blivepost.services.dynamics import (
        _DynamicPollSession, load_seen_dynamic_ids,
    )

    q = queue.Queue()
    seen = load_seen_dynamic_ids(111, str(tmp_path))
    session = _DynamicPollSession(111, seen, q, dynamics_dir=str(tmp_path))
    # 基线：旧置顶100 + 最新200
    session.handle({"data": {"items": [
        {"id_str": "100", "modules": {"module_tag": {"text": "置顶"},
                                      "module_author": {}}},
        {"id_str": "200", "modules": {"module_author": {}}},
    ]}})
    assert q.empty()
    # 新动态300发布即置顶（比最新200新）→ 立即推送300
    session.handle({"data": {"items": [
        {"id_str": "300", "modules": {"module_tag": {"text": "置顶"},
                                      "module_author": {}}},
        {"id_str": "200", "modules": {"module_author": {}}},
    ]}})
    item = q.get_nowait()
    assert item["dynamic_id"] == "300"


# ==================== 2 子进程日志分离 ====================


def _file_handlers():
    root = logging.getLogger("blivepost")
    return [h for h in root.handlers if isinstance(h, DailyRotatingFileHandler)]


def test_setup_log_suffix_in_filename(tmp_path):
    """log_suffix产生含进程标识的独立日志文件名"""
    Logger().setup(name="blivepost", log_dir=str(tmp_path), console=False,
                   log_suffix="room_23058")
    try:
        handlers = _file_handlers()
        assert handlers, "应创建文件处理器"
        assert all("blivepost_room_23058" in h.baseFilename for h in handlers)
        # 文件随handler创建即落盘
        files = list(tmp_path.glob("blivepost_room_23058.*.log"))
        assert len(files) == 1
    finally:
        Logger().setup(name="blivepost", console=True)  # 还原默认


def test_setup_default_filename_unchanged(tmp_path):
    """不传log_suffix时保持默认文件名（向后兼容）"""
    Logger().setup(name="blivepost", log_dir=str(tmp_path), console=False)
    try:
        handlers = _file_handlers()
        assert handlers
        assert all("blivepost_room" not in h.baseFilename
                   and "blivepost_dyn" not in h.baseFilename
                   for h in handlers)
        files = [p.name for p in tmp_path.glob("*.log")]
        assert all(f.startswith("blivepost.") for f in files)
    finally:
        Logger().setup(name="blivepost", console=True)


def test_room_process_main_passes_log_suffix(monkeypatch):
    """房间子进程入口用独立日志后缀room_<id>"""
    from blivepost.core import room_process as rp
    from blivepost.utils.logger import Logger

    recorded = {}

    def fake_setup(self, name="blivepost", level=None, log_dir=None,
                   console=True, debug=False, log_suffix=None):
        recorded.update(log_dir=log_dir, log_suffix=log_suffix)
        return logging.getLogger(name)

    monkeypatch.setattr(Logger, "setup", fake_setup)
    monkeypatch.setattr(rp.asyncio, "run", lambda coro: coro.close())

    rp._room_process_main(23058, Settings(), None, None, log_dir="dlogs")
    assert recorded["log_suffix"] == "room_23058"
    assert recorded["log_dir"] == "dlogs"


def test_dynamic_process_main_passes_log_suffix(monkeypatch, tmp_path):
    """动态子进程入口用独立日志后缀dyn_<uid>，未指定目录时回落默认logs"""
    from blivepost.services import dynamics as dyn
    from blivepost.utils.logger import Logger

    recorded = {}

    def fake_setup(self, name="blivepost", level=None, log_dir=None,
                   console=True, debug=False, log_suffix=None):
        recorded.update(log_dir=log_dir, log_suffix=log_suffix)
        return logging.getLogger(name)

    class StoppedEvent:
        def is_set(self):
            return True  # 轮询主循环立即退出

    monkeypatch.setattr(Logger, "setup", fake_setup)
    dyn._dynamic_process_main(111, FakeQueue(), StoppedEvent(),
                              dynamics_dir=str(tmp_path), log_dir="dlogs")
    assert recorded["log_suffix"] == "dyn_111"
    assert recorded["log_dir"] == "dlogs"

    # 未显式指定log_dir：回落默认logs目录（仍为独立文件名）
    dyn._dynamic_process_main(111, FakeQueue(), StoppedEvent(),
                              dynamics_dir=str(tmp_path))
    assert recorded["log_suffix"] == "dyn_111"
    assert recorded["log_dir"] == dyn.DEFAULT_LOG_DIR


async def test_consumer_dynamic_alert_notifies_admins():
    """dynamic_alert队列消息→私聊管理员（连续异常告警接线）"""
    from blivepost.config.settings import Settings
    from blivepost.core.consumer import PushQueueConsumer
    from blivepost.services.push import PushService

    class NotifyBot:
        def __init__(self):
            self.sent = []

        async def notify_admins(self, targets, content):
            self.sent.append((list(targets), content))
            return 1

    ps = PushService(Settings())
    ps.bot_client = NotifyBot()
    consumer = PushQueueConsumer(None, ps, None,
                                 admin_openids=["ADM1"], binding_store=None)
    await consumer._handle({"type": "dynamic_alert", "up_uid": 111,
                            "consecutive": 10})
    assert ps.bot_client.sent[0][0] == ["ADM1"]
    assert "111" in ps.bot_client.sent[0][1] and "10" in ps.bot_client.sent[0][1]


def test_requests_fetch_signed_blocked_falls_back_unsigned(monkeypatch):
    """签名请求被风控(412/非JSON)→自动回退无签名请求并成功解析（2026-11-10真机实证场景）"""
    import blivepost.services.dynamics as dyn

    class _FakeSigner:
        def sign_url_params(self, params):
            return "deadbeef", "1700000000"

    calls = []

    class _Resp:
        def __init__(self, payload, status=200, text=None):
            self._payload, self.status, self.text = payload, status, text or ""

        def json(self):
            if self._payload is None:
                raise ValueError("Expecting value: line 1 column 1 (char 0)")
            return self._payload

    ok_payload = {"code": 0, "data": {"items": [
        {"id_str": "900", "modules": {
            "module_tag": {"text": "置顶"},
            "module_author": {"name": "UP", "pub_ts": 1}}},
        {"id_str": "901", "modules": {
            "module_author": {"name": "UP", "pub_ts": 2}}},
    ]}}

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if "w_rid=" in url:
            return _Resp(None, status=412, text="<!DOCTYPE html>...")  # 风控HTML
        return _Resp(ok_payload)

    monkeypatch.setattr(dyn.requests, "get", fake_get)
    data = dyn._requests_fetch(111, cookie_string="buvid3=X; SESSDATA=Y",
                               wbi=_FakeSigner())
    ids = [i["id_str"] for i in data["data"]["items"]]      # 原始feed dict
    assert "901" in ids
    assert len(calls) == 2 and "w_rid=" in calls[0] and "w_rid=" not in calls[1]
    assert calls[1] == calls[0].split("&wts=")[0]           # 去签名URL


def test_requests_fetch_both_fail_returns_none(monkeypatch):
    """签名与无签名均失败→None（计异常轮，触发连续告警）"""
    import blivepost.services.dynamics as dyn

    class _FakeSigner:
        def sign_url_params(self, params):
            return "ab", "12"

    class _Resp:
        status = 412

        def json(self):
            raise ValueError("Expecting value")

    monkeypatch.setattr(dyn.requests, "get",
                        lambda url, headers=None, timeout=None: _Resp())
    assert dyn._requests_fetch(111, cookie_string="c", wbi=_FakeSigner()) is None


def test_requests_fetch_sends_full_cookie(monkeypatch):
    """请求头携带完整cookie（buvid3等风控必需项），而非仅SESSDATA"""
    import blivepost.services.dynamics as dyn

    class _FakeSigner:
        def sign_url_params(self, params):
            return "ab", "12"

    captured = {}

    class _Resp:
        def json(self):
            return {"code": 0, "data": {"items": []}}

    def fake_get(url, headers=None, timeout=None):
        captured["cookie"] = headers.get("cookie")
        return _Resp()

    monkeypatch.setattr(dyn.requests, "get", fake_get)
    dyn._requests_fetch(111, cookie_string="buvid3=X; SESSDATA=Y", wbi=_FakeSigner())
    assert captured["cookie"] == "buvid3=X; SESSDATA=Y"


async def test_bind_dynamic_normalizes_room_id_to_uid():
    """/绑定动态 填房间号→自动换算UP的UID（2026-11-10真机以房间号订阅致轮询错UID）"""
    from blivepost.services.commands import CommandHandler

    class Store:
        def __init__(self):
            self.bound = []

        async def bind_dynamic(self, group, up_uid):
            self.bound.append(up_uid)
            return True

        def get_dynamic_up_name(self, up_uid):
            return ""

    class Mgr:
        enabled = True

        def __init__(self):
            self.ensured = []

        async def ensure(self, up_uid, baseline_first=False):
            self.ensured.append((up_uid, baseline_first))
            return True, None

    class Db:
        async def get_room_uid(self, room_id):
            return 36296621 if room_id == 3060820 else 0

    store, mgr = Store(), Mgr()
    ch = CommandHandler(store, None, db_service=Db(), dynamic_manager=mgr)
    reply = await ch._bind_dynamic("G1", 3060820)          # 输入的是房间号
    assert store.bound == [36296621]                        # 绑定的是UID
    assert mgr.ensured[0][0] == 36296621
    assert "已自动换算为UP主UID" in reply


# ---------- 全局单浏览器取数服务 ----------

async def test_dyn_fetch_service_auth_and_url_guard(monkeypatch):
    """取数服务：token校验401；URL白名单403；正常请求回传浏览器结果"""
    from aiohttp.test_utils import TestClient, TestServer
    from blivepost.services.dyn_fetch import DynFetchService

    svc = DynFetchService()

    class FakeHolder:
        async def fetch_json(self, url):
            return {"code": 0, "data": {"items": [{"id_str": "1"}]}}

        async def close(self):
            pass

        def set_cookies(self, s):
            pass

    svc._holder = FakeHolder()
    client = TestClient(TestServer(svc._build_app()))
    await client.start_server()
    try:
        # 无token → 401
        r = await client.post("/dyn-fetch", json={"url": "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"})
        assert r.status == 401
        # 非白名单URL → 403
        r = await client.post("/dyn-fetch", json={"url": "http://evil.com/x"},
                              headers={"X-Fetch-Token": svc.token})
        assert r.status == 403
        # 正常 → 浏览器结果data
        r = await client.post(
            "/dyn-fetch",
            json={"url": "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?host_mid=1"},
            headers={"X-Fetch-Token": svc.token})
        assert r.status == 200
        body = await r.json()
        assert body["data"]["data"]["items"][0]["id_str"] == "1"
        # 浏览器失败 → 502
        async def fail(url):
            return None
        svc._holder.fetch_json = fail
        r = await client.post(
            "/dyn-fetch",
            json={"url": "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?host_mid=1"},
            headers={"X-Fetch-Token": svc.token})
        assert r.status == 502
    finally:
        await client.close()


async def test_remote_fetch_proxy(monkeypatch):
    """子进程侧代理：正常回传data；异常/非200回None（触发requests兜底）"""
    import blivepost.services.dynamics as dyn

    proxy = dyn._RemoteFetchProxy(18080, "TK")
    calls = []

    class FakeResp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload

        def json(self):
            return self._payload

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers.get("X-Fetch-Token")))
        return FakeResp(200, {"data": {"code": 0, "x": 1}})

    monkeypatch.setattr(dyn.requests, "post", fake_post)
    assert proxy.fetch_json("https://api.bilibili.com/x/polymer/web-dynamic/v1/a") == {"code": 0, "x": 1}
    assert calls[0][0] == "http://127.0.0.1:18080/dyn-fetch"
    assert calls[0][2] == "TK"

    def fail_post(*a, **kw):
        raise dyn.requests.RequestException("服务不可达")

    monkeypatch.setattr(dyn.requests, "post", fail_post)
    assert proxy.fetch_json("u") is None
    monkeypatch.setattr(dyn.requests, "post",
                        lambda *a, **kw: FakeResp(502, {}))
    assert proxy.fetch_json("u") is None


# ---------- 双开关语义统一（2026-11-10真机反馈） ----------

async def test_bind_dynamic_respects_runtime_switch(monkeypatch):
    """运行时开关关：/绑定动态 只记录订阅不启轮询，并明确提示"""
    from blivepost.services.commands import CommandHandler

    class Store:
        def __init__(self):
            self.bound = []

        async def bind_dynamic(self, group, up_uid):
            self.bound.append(up_uid)
            return True

        def get_dynamic_up_name(self, up_uid):
            return ""

    class Mgr:
        enabled = True

        def __init__(self):
            self.ensured = []

        async def ensure(self, up_uid, baseline_first=False):
            self.ensured.append(up_uid)
            return True, None

    class Db:
        async def get_push_switch_enabled(self, name, default=False):
            return False          # 运行时开关=关

        async def get_room_uid(self, room_id):
            return 0

    store, mgr = Store(), Mgr()
    ch = CommandHandler(store, None, db_service=Db(), dynamic_manager=mgr)
    reply = await ch._bind_dynamic("G1", 12345)
    assert store.bound == [12345]            # 订阅已记录（开关打开后自动恢复）
    assert mgr.ensured == []                 # 但未启动轮询
    assert "推送开关当前为关" in reply

    class DbOn(Db):
        async def get_push_switch_enabled(self, name, default=False):
            return True

    ch2 = CommandHandler(store, None, db_service=DbOn(), dynamic_manager=mgr)
    reply2 = await ch2._bind_dynamic("G1", 12345)
    assert mgr.ensured == [12345]            # 开关开：正常启动轮询
    assert "推送开关当前为关" not in reply2


async def test_status_includes_dynamic_section():
    """/状态 输出含动态订阅段与轮询状态；开关关时附警示"""
    from blivepost.services.commands import CommandHandler

    class B:
        room_id = 3060820
        uid = 36296621
        monitored = True

    class Store:
        def get_group_bindings(self, group):
            return [B()]

        def get_group_dynamic_ups(self, group):
            return [36296621]

        def get_dynamic_up_name(self, uid):
            return "洛沐语"

    class Mgr:
        enabled = True

        def has(self, uid):
            return True

        def room_ids(self):
            return [3060820]

    class Db:
        async def get_push_switch_enabled(self, name, default=False):
            return False

    ch = CommandHandler(Store(), Mgr(), db_service=Db(), dynamic_manager=Mgr())
    text = await ch._status("G1")
    assert "订阅的UP主动态" in text
    assert "UP主36296621（洛沐语） - 轮询运行中" in text
    assert "推送开关当前为关" in text


def test_parse_feed_data_string_pub_ts():
    """pub_ts为字符串（接口类型变更，真机2026-11-10 TypeError致推送失败）→ 强转int"""
    import blivepost.services.dynamics as dyn
    payload = {"code": 0, "data": {"items": [
        {"id_str": "901", "modules": {
            "module_author": {"name": "UP", "pub_ts": "1762765229"}}},
    ]}}
    infos = dyn._parse_feed_data(payload, 111)
    assert infos[0]["pub_ts"] == 1762765229          # str → int
    assert dyn._to_int(None) == 0 and dyn._to_int("x") == 0
    assert dyn._to_int("123.9") == 123


async def test_consumer_dynamic_str_pub_ts_pushes():
    """consumer端pub_ts字符串防御：推送链不因类型抛错"""
    from blivepost.config.settings import Settings
    from blivepost.core.consumer import PushQueueConsumer
    from blivepost.services.push import PushService

    calls = []

    class Bot:
        def push_media(self, image, content, groups, keyboard=None,
                       at_all_groups=None):
            calls.append(content)
            return True

        def broadcast(self, groups, content, keyboard=None, at_all_groups=None):
            calls.append(content)
            return True

    ps = PushService(Settings())
    ps.bot_client = Bot()

    class Store:
        def get_dynamic_groups(self, up_uid):
            return ["G1"]

        def get_dynamic_up_name(self, up_uid):
            return "UP"

    consumer = PushQueueConsumer(None, ps, None, binding_store=Store(),
                                 switch_defaults={"dynamic_monitor": True})
    await consumer._handle_dynamic({
        "type": "dynamic", "up_uid": 111, "dynamic_id": "900",
        "author": "UP", "pub_ts": "1762765229",
        "summary": "内容", "image": "http://img.png"})
    assert len(calls) == 1 and "UP" in calls[0]     # 推送成功未抛TypeError


# ---------- opus渲染与配图决策（/dyn-prepare） ----------

async def test_dyn_prepare_image_priority(monkeypatch):
    """配图优先级（2026-11-10用户调整）：截图→卡片(含头像)→原图URL→无图"""
    from aiohttp.test_utils import TestClient, TestServer
    from blivepost.services.dyn_fetch import DynFetchService
    import base64

    content = {"author": "UP", "avatar": "https://i0.hdslb.com/bfs/face/a.jpg",
               "title": "标题", "text": "正文内容",
               "images": ["https://i0.hdslb.com/bfs/a.png"]}
    card_bytes = b"PNGCARD"

    monkeypatch.setattr(DynFetchService, "_render_card",
                        staticmethod(lambda *a: card_bytes))

    async def fake_render(dynamic_id):
        return dict(content)

    svc = DynFetchService()
    svc._render_opus = fake_render
    client = TestClient(TestServer(svc._build_app()))
    await client.start_server()
    try:
        # ① 截图可用 → 优先
        async def fake_shot():
            return b"PNGSHOT"

        svc._screenshot_view = fake_shot
        r = await client.post("/dyn-prepare", json={"dynamic_id": "900"},
                              headers={"X-Fetch-Token": svc.token})
        body = await r.json()
        assert body["image_source"] == "screenshot"
        assert base64.b64decode(body["image_b64"]) == b"PNGSHOT"
        assert body["image_url"] is None

        # 截图失败 → ② PIL卡片（含头像）
        async def no_shot():
            return None

        svc._screenshot_view = no_shot
        r = await client.post("/dyn-prepare", json={"dynamic_id": "900"},
                              headers={"X-Fetch-Token": svc.token})
        body = await r.json()
        assert body["image_source"] == "card"
        assert base64.b64decode(body["image_b64"]) == card_bytes

        # 截图超大 + 卡片不可用 → ③原图URL
        monkeypatch.setattr(DynFetchService, "_render_card",
                            staticmethod(lambda *a: None))

        async def big_shot():
            return b"x" * (5 * 1024 * 1024)

        svc._screenshot_view = big_shot
        r = await client.post("/dyn-prepare", json={"dynamic_id": "900"},
                              headers={"X-Fetch-Token": svc.token})
        body = await r.json()
        assert body["image_source"] == "original"
        assert body["image_url"] == content["images"][0]
        assert body["image_b64"] is None

        # 非法dynamic_id → 400；渲染失败无原图 → 无图
        r = await client.post("/dyn-prepare", json={"dynamic_id": "../evil"},
                              headers={"X-Fetch-Token": svc.token})
        assert r.status == 400

        async def fail_render(dynamic_id):
            return None
        svc._render_opus = fail_render
        r = await client.post("/dyn-prepare", json={"dynamic_id": "901"},
                              headers={"X-Fetch-Token": svc.token})
        body = await r.json()
        assert body["image_url"] is None and body["image_source"] == "none"
    finally:
        await client.close()


def test_render_card_layout_no_empty_title_gap():
    """卡片排版：标题为空时不产生空行/多余间距（修复'两行正文'反馈）；
    卡片可渲染且为PNG字节"""
    from blivepost.services.dyn_fetch import DynFetchService
    card = DynFetchService._render_card("UP", "", "只有正文没有标题")
    assert card is not None and card[1:4] == b"PNG"  # PNG魔数

    card2 = DynFetchService._render_card("UP", "有标题", "有正文")
    assert card2 is not None


async def test_remote_proxy_prepare():
    """子进程代理prepare：content→推送字段映射（含b64互斥）"""
    import blivepost.services.dynamics as dyn

    proxy = dyn._RemoteFetchProxy(18080, "TK")
    b64 = "iVBORw0KGgo="

    class FakeResp:
        status_code = 200

        def json(self):
            return {"content": {"author": "UP", "title": "标题",
                                "text": "正文", "images": ["http://i/1.jpg"]},
                    "image_b64": b64, "image_url": None,
                    "image_source": "card"}

    orig_post = dyn.requests.post
    dyn.requests.post = lambda *a, **kw: FakeResp()
    try:
        prepared = proxy.prepare("900")
        assert prepared["author"] == "UP"
        assert "标题" in prepared["summary"] and "正文" in prepared["summary"]
        assert prepared["image_b64"] == b64 and prepared["image"] is None
        assert prepared["image_source"] == "card"
    finally:
        dyn.requests.post = orig_post


async def test_consumer_dynamic_local_image_binary_push():
    """consumer：b64本地图→bot.push_media_data(bytes)；URL图→push_media"""
    from blivepost.config.settings import Settings
    from blivepost.core.consumer import PushQueueConsumer
    from blivepost.services.push import PushService
    import base64

    calls = []

    class Bot:
        def push_media_data(self, data, content, groups, file_name=None):
            calls.append(("data", data))
            return True

        def push_media(self, image, content, groups, keyboard=None,
                       at_all_groups=None):
            calls.append(("url", image))
            return True

        def broadcast(self, groups, content, keyboard=None, at_all_groups=None):
            calls.append(("text", None))
            return True

    ps = PushService(Settings())
    ps.bot_client = Bot()

    class Store:
        def get_dynamic_groups(self, up_uid):
            return ["G1"]

        def get_dynamic_up_name(self, up_uid):
            return "UP"

    consumer = PushQueueConsumer(None, ps, None, binding_store=Store(),
                                 switch_defaults={"dynamic_monitor": True})
    payload = {"type": "dynamic", "up_uid": 111, "dynamic_id": "900",
               "author": "UP", "pub_ts": 1762765229, "summary": "内容",
               "image": None, "image_b64": base64.b64encode(b"PNG").decode(),
               "image_source": "card"}
    await consumer._handle_dynamic(payload)
    assert calls == [("data", b"PNG")]          # 本地图直传，未走URL/文本链

    calls.clear()
    payload.update({"image": "http://img/1.png", "image_b64": None,
                    "image_source": "original"})
    await consumer._handle_dynamic(payload)
    assert calls == [("url", "http://img/1.png")]
