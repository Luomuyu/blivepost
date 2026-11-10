# -*- coding: utf-8 -*-
"""动态轮询遗留修复回归测试：

- 1a WbiSigner复用：轮询循环内复用签名器（TTL惰性重建+失败重建），
     不再每轮新建（每轮多发nav请求）；TTL判定用注入时钟，不依赖真实墙钟
- 1b 漏推中间动态：一轮取全部未见过的动态，按发布时间从旧到新逐条上报
- 1c 风控/异常告警：连续异常（请求失败/风控）超阈值经队列告警一次（恢复
     重置、不重复）；正常空列表是有效响应，不计失败
- 1d 重新订阅基线化：baseline_first=True时首轮无条件记seen不补推
     （防止停订期间的新动态被一次性补推）；seen修剪防膨胀；置顶动态
     延迟推为已知行为
- 2  子进程日志分离：setup产生的日志文件名含进程标识（room_<id>/dyn_<uid>）
"""

import asyncio
import logging

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


def test_poll_once_reuses_shared_signer_across_rounds(monkeypatch):
    """_poll_once多轮共用同一签名器缓存：WbiSigner仅构建一次"""
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
    assert dyn._poll_once(111, "", cache) is not None
    assert dyn._poll_once(111, "", cache) is not None
    assert CountingSigner.built == 1  # 两轮轮询未重复构建签名器


# ==================== 1b 漏推中间动态 ====================


def test_poll_once_returns_all_items_and_skips_pinned(monkeypatch):
    """轮询返回全部条目（置顶跳过），不再只看最新一条"""
    from blivepost.services import dynamics as dyn

    pinned = _item("100", pub_ts=1600000000)
    pinned["modules"]["module_tag"] = {"text": "置顶"}
    items = [pinned, _item("901", pub_ts=1700000002),
             _item("900", pub_ts=1700000001)]
    monkeypatch.setattr(dyn.requests, "get",
                        lambda *a, **kw: _feed_response(items))

    infos = dyn._poll_once(111)
    assert [i["dynamic_id"] for i in infos] == ["901", "900"]


def test_poll_session_pushes_all_unseen_oldest_first(tmp_path):
    """短时间连发多条：全部未见动态按发布时间从旧到新逐条推送"""
    from blivepost.services.dynamics import (
        _DynamicPollSession, load_seen_dynamic_ids,
    )

    q = FakeQueue()
    seen = {"800"}  # 预置旧动态（baseline随之关闭）
    session = _DynamicPollSession(111, seen, q, dynamics_dir=str(tmp_path))

    # feed新到旧：新动态B、已见旧动态、新动态A
    session.handle([
        {"dynamic_id": "902", "author": "UP甲", "pub_ts": 1700000002,
         "summary": "第二条", "image": ""},
        {"dynamic_id": "800", "author": "UP甲", "pub_ts": 1700000000,
         "summary": "旧", "image": ""},
        {"dynamic_id": "901", "author": "UP甲", "pub_ts": 1700000001,
         "summary": "第一条", "image": ""},
    ])

    dynamics = [i for i in q.items if i["type"] == "dynamic"]
    assert [i["dynamic_id"] for i in dynamics] == ["901", "902"]  # 从旧到新
    assert all(i["up_uid"] == 111 for i in dynamics)
    # 判重持久化：重启后重新加载包含全部新ID
    assert load_seen_dynamic_ids(111, str(tmp_path)) >= {"901", "902"}


def test_poll_session_baseline_batch_not_pushed(tmp_path):
    """空库首轮：首批动态仅作基线记录不推送，其后新动态正常推送"""
    from blivepost.services.dynamics import _DynamicPollSession

    q = FakeQueue()
    session = _DynamicPollSession(111, set(), q, dynamics_dir=str(tmp_path))
    session.handle([{"dynamic_id": "900", "author": "UP", "pub_ts": 1,
                     "summary": "", "image": ""}])
    assert q.items == []  # 基线不推送
    session.handle([{"dynamic_id": "901", "author": "UP", "pub_ts": 2,
                     "summary": "", "image": ""}])
    assert [i["dynamic_id"] for i in q.items] == ["901"]


# ==================== 1c 风控/异常告警 ====================


def test_poll_once_risk_control_code_returns_none(monkeypatch):
    """风控/错误响应码返回None，正常空数据返回空列表（两者区分）"""
    from blivepost.services import dynamics as dyn

    monkeypatch.setattr(dyn.requests, "get",
                        lambda *a, **kw: _feed_response([], code=-352))
    assert dyn._poll_once(111) is None  # 风控响应

    monkeypatch.setattr(dyn.requests, "get",
                        lambda *a, **kw: _feed_response([], code=0))
    assert dyn._poll_once(111) == []  # 真实空数据


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

    # 恢复正常：重置计数
    session.handle([{"dynamic_id": "901", "author": "UP", "pub_ts": 1,
                     "summary": "", "image": ""}])
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

    session.handle([])  # 空列表=有效响应：重置
    assert session.fail_streak == 0

    for _ in range(5):  # 持续空列表（UP删光动态等）不会触发告警
        session.handle([])
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
    session.handle([{"dynamic_id": "901", "author": "UP", "pub_ts": 1,
                     "summary": "", "image": ""}])
    session.handle(None)
    assert session.fail_streak == 1
    assert all(i["type"] != "dynamic_alert" for i in q.items)


# ==================== 1d 重新订阅基线化 / seen修剪 / 置顶延迟推 ====================


def test_resubscribe_baseline_first_suppresses_catchup_push(tmp_path):
    """重新订阅（seen非空+baseline_first=True）：首轮无条件基线化，
    停订期间的新动态一次性记为seen、不补推；其后新动态正常推送
    （对照：默认baseline_first=False时同样输入会立即补推——见
    test_poll_session_pushes_all_unseen_oldest_first）"""
    from blivepost.services.dynamics import (
        _DynamicPollSession, load_seen_dynamic_ids,
    )

    q = FakeQueue()
    seen = {"800"}  # 停订前已见（seen表非空）
    session = _DynamicPollSession(111, seen, q, dynamics_dir=str(tmp_path),
                                  baseline_first=True)

    # 重订阅首轮：停订期间发布的动态全部可见
    session.handle([
        {"dynamic_id": "901", "author": "UP甲", "pub_ts": 1700000001,
         "summary": "停订期间", "image": ""},
        {"dynamic_id": "900", "author": "UP甲", "pub_ts": 1700000000,
         "summary": "停订期间", "image": ""},
    ])
    assert q.items == []  # 基线化：不补推
    assert load_seen_dynamic_ids(111, str(tmp_path)) >= {"900", "901"}

    # 其后新发布的动态正常推送
    session.handle([{"dynamic_id": "902", "author": "UP甲", "pub_ts": 1700000002,
                     "summary": "新动态", "image": ""}])
    assert [i["dynamic_id"] for i in q.items] == ["902"]


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
    assert recorded["args"][-1] is True

    await m.ensure(222)  # 默认False：运行中恢复（重启/开关恢复/崩溃重建）语义
    assert recorded["args"][0] == 222
    assert recorded["args"][-1] is False

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


def test_pinned_dynamic_pushed_only_after_unpin(monkeypatch, tmp_path):
    """已知行为回归锚点：发布即置顶的动态在置顶期间被跳过、不记seen；
    取消置顶后重新出现在feed中时作为"新动态"补推（延迟推送）"""
    from blivepost.services import dynamics as dyn
    from blivepost.services.dynamics import _DynamicPollSession

    def feed(tagged):
        item = _item("900", pub_ts=1700000000)
        if tagged:
            item["modules"]["module_tag"] = {"text": "置顶"}
        return _feed_response([item])

    q = FakeQueue()
    session = _DynamicPollSession(111, {"800"}, q, dynamics_dir=str(tmp_path))

    monkeypatch.setattr(dyn.requests, "get", lambda *a, **kw: feed(True))
    session.handle(dyn._poll_once(111))  # 置顶期间：被跳过
    assert q.items == []
    assert "900" not in session.seen  # 未记seen（延迟推的机制所在）

    monkeypatch.setattr(dyn.requests, "get", lambda *a, **kw: feed(False))
    session.handle(dyn._poll_once(111))  # 取消置顶：作为新动态补推
    assert [i["dynamic_id"] for i in q.items] == ["900"]


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


def test_poll_once_signed_blocked_falls_back_unsigned(monkeypatch):
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
    infos = dyn._poll_once(111, cookie_string="buvid3=X; SESSDATA=Y",
                           wbi=_FakeSigner())
    assert [i["dynamic_id"] for i in infos] == ["901"]      # 置顶跳过
    assert len(calls) == 2 and "w_rid=" in calls[0] and "w_rid=" not in calls[1]
    assert calls[1] == calls[0].split("&wts=")[0]           # 去签名URL


def test_poll_once_both_fail_returns_none(monkeypatch):
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
    assert dyn._poll_once(111, cookie_string="c", wbi=_FakeSigner()) is None


def test_poll_once_sends_full_cookie(monkeypatch):
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
    dyn._poll_once(111, cookie_string="buvid3=X; SESSDATA=Y", wbi=_FakeSigner())
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
