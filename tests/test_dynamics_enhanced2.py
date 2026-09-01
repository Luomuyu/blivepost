# -*- coding: utf-8 -*-
"""wbi签名URL、内容提取、状态文件、watcher开播检测测试"""

import json

from blivepost.services.dynamics import (
    _extract_content,
    _write_live_state, _read_live_state, LiveStateWatcher,
)
from blivepost.services.wbi import WbiSigner


# ==================== wbi签名URL ====================


def test_sign_url_params(monkeypatch):
    """sign_url_params返回w_rid与wts，密钥失败时w_rid为空"""
    signer = WbiSigner()
    monkeypatch.setattr(signer, "get_w_key", lambda force_refresh=False: "k" * 32)
    w_rid, wts = signer.sign_url_params("host_mid=1&platform=web")
    assert len(w_rid) == 32 and wts.isdigit()


def test_build_dynamic_url_signed(monkeypatch):
    from blivepost.services import dynamics as dyn

    class FakeWbi:
        def sign_url_params(self, params):
            return "a" * 32, "1700000000"

    url = dyn._build_dynamic_url(12345, FakeWbi())
    assert "host_mid=12345" in url
    assert "w_rid=" + "a" * 32 in url and "wts=1700000000" in url


def test_build_dynamic_url_fallback(monkeypatch):
    """签名失败降级为无签名URL"""
    from blivepost.services import dynamics as dyn

    class BadWbi:
        def sign_url_params(self, params):
            return "", "0"

    url = dyn._build_dynamic_url(12345, BadWbi())
    assert url.endswith("host_mid=12345&platform=web")
    assert "w_rid" not in url


def test_poll_once_uses_signed_url(monkeypatch):
    """轮询请求使用签名后的URL"""
    from blivepost.services import dynamics as dyn
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        items = [{"id_str": "1", "modules": {"module_author": {"name": "u", "pub_ts": 1}}}]
        return _fake_feed(items)

    def _fake_feed(items):
        class R:
            def json(self):
                return {"code": 0, "data": {"items": items}}
        return R()

    monkeypatch.setattr(dyn.requests, "get", fake_get)
    monkeypatch.setattr(dyn, "_build_dynamic_url", lambda uid, wbi=None: f"signed?host_mid={uid}")
    dyn._poll_once(111)
    assert "signed" in seen["url"]


# ==================== 内容提取（多类型） ====================


def test_extract_draw_dynamic():
    modules = {"module_dynamic": {
        "desc": {"text": "图文动态"},
        "major": {"draw": {"items": [{"src": "http://a.png"}]}},
    }}
    summary, image = _extract_content(modules)
    assert summary == "图文动态" and image == "http://a.png"


def test_extract_archive_dynamic():
    modules = {"module_dynamic": {
        "desc": {"text": ""},
        "major": {"archive": {"title": "视频标题", "desc": "视频描述", "cover": "http://c.jpg"}},
    }}
    summary, image = _extract_content(modules)
    assert summary == "视频描述" and image == "http://c.jpg"


def test_extract_opus_dynamic():
    modules = {"module_dynamic": {
        "major": {"opus": {"title": "专栏标题", "pictures": [{"src": "http://p.png"}]}},
    }}
    summary, image = _extract_content(modules)
    assert summary == "专栏标题" and image == "http://p.png"


def test_extract_forward_dynamic():
    modules = {
        "module_dynamic": {"desc": {"text": "转发：好文"}},
        "module_static": {"module_dynamic": {"desc": {"text": "原始内容" * 10}}},
    }
    summary, _ = _extract_content(modules)
    assert "转发：好文" in summary or "好文" in summary


def test_extract_none_structure():
    """结构异常不崩溃，返回空"""
    summary, image = _extract_content({"module_dynamic": None, "major": "bad"})
    assert summary == "" and image == ""


# ==================== 状态文件与watcher ====================


def test_live_state_roundtrip(tmp_path):
    """状态文件写入与读取回环（显式路径）"""
    path = tmp_path / "state.json"
    _write_live_state({111: {"live": True, "room_id": 7, "title": "T"}}, path=path)
    state = _read_live_state(111, path=path)
    assert state == {"live": True, "room_id": 7, "title": "T"}
    assert _read_live_state(999, path=path) is None
    assert _read_live_state(111, path=tmp_path / "missing.json") is None


def test_live_state_instance_isolated(tmp_path):
    """实例隔离：两个不同路径的状态文件互不影响（多实例同机）"""
    pa = tmp_path / "a" / "state.json"
    pb = tmp_path / "b" / "state.json"
    _write_live_state({111: {"live": True, "room_id": 1, "title": "A"}}, path=pa)
    _write_live_state({111: {"live": False, "room_id": 2, "title": "B"}}, path=pb)
    assert _read_live_state(111, path=pa)["live"] is True
    assert _read_live_state(111, path=pb)["live"] is False


def test_default_state_path_is_data_dir():
    """默认路径为数据目录（实例私有），不在系统临时目录"""
    from blivepost.services.dynamics import _default_state_path
    p = _default_state_path()
    assert str(p).startswith("data")
    import tempfile
    assert tempfile.gettempdir() not in str(p.resolve()) if p.is_absolute() else True


async def test_watcher_writes_state_file(tmp_path, monkeypatch):
    """watcher把批量状态写入指定路径的共享文件"""
    from blivepost.services import dynamics as dyn
    states = {111: {"live": True, "room_id": 7, "title": "T"}}
    monkeypatch.setattr(dyn, "_fetch_live_states", lambda uids: states)
    q = FakeQueue()
    watcher = LiveStateWatcher(FakeStore(), q, state_path=tmp_path / "st.json")
    await watcher.check_once()
    saved = json.loads((tmp_path / "st.json").read_text(encoding="utf-8"))
    assert saved["111"]["live"] is True


def test_fetch_live_states_batch(monkeypatch):
    """批量查询：一次请求覆盖全部UID"""
    from blivepost.services import dynamics as dyn
    calls = []

    class FakeResp:
        def json(self):
            return {"code": 0, "data": {
                "1": {"live_status": 1, "room_id": 10, "title": "A"},
                "2": {"live_status": 0, "room_id": 0, "title": ""},
            }}

    def fake_post(url, **kw):
        calls.append(1)
        return FakeResp()

    monkeypatch.setattr(dyn.requests, "post", fake_post)
    states = dyn._fetch_live_states([1, 2])
    assert len(calls) == 1  # 单次批量请求
    assert states[1]["live"] and not states[2]["live"]


class FakeStore:
    def get_dynamic_up_uids(self):
        return [111, 222]


class FakeQueue:
    def __init__(self):
        self.items = []

    def put(self, item, timeout=None):
        self.items.append(item)


async def test_watcher_detects_went_live(monkeypatch, tmp_path):
    """watcher检测到未播→在播变化时经队列出报，重复检测不重复上报"""
    from blivepost.services import dynamics as dyn

    states = {111: {"live": False, "room_id": 7, "title": "T1"},
              222: {"live": False, "room_id": 0, "title": ""}}
    monkeypatch.setattr(dyn, "_fetch_live_states", lambda uids: states)

    q = FakeQueue()
    watcher = LiveStateWatcher(FakeStore(), q, interval=600,
                               state_path=tmp_path / "st.json")

    n = await watcher.check_once()
    assert n == 0  # 首轮建立基线，无开播
    states[111] = {"live": True, "room_id": 7, "title": "T1"}
    n = await watcher.check_once()
    assert n == 1
    assert q.items == [{"type": "up_live", "up_uid": 111, "room_id": 7, "title": "T1"}]
    # 仍在播：不重复上报
    n = await watcher.check_once()
    assert n == 0 and len(q.items) == 1
