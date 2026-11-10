# -*- coding: utf-8 -*-
"""阶段2（安全加固）回归测试

覆盖：
1. 登录防爆破：IP/用户名失败计数与临时锁定
2. 扫码登录会话隔离：他人不可查看/取消我的二维码
3. web_token：常量时间比较；长效令牌不再落cookie、cookie不再被接受
4. 数据导出默认脱敏（VACUUM INTO快照+清cookie_account），显式参数保留凭证
5. /api/status 需登录（不再对游客暴露账号情报）
6. 签名会话持久化：同token跨"重启"有效；篡改/过期验签失败；无token两实例
   密钥不同；web_token变更=紧急全量登出
"""

import io
import sqlite3
import zipfile

import pytest

from blivepost.services.webpanel import WebPanel


def _make_panel(token="", admin_password=""):
    return WebPanel(None, None, None, None, None,
                    token=token, admin_password=admin_password)


class FakeReq:
    def __init__(self, query=None, headers=None, cookies=None, form=None, path="/"):
        self.query = query or {}
        self.headers = headers or {}
        self.cookies = cookies or {}
        self._form = form or {}
        self.path = path
        self.remote = "1.2.3.4"

    async def post(self):
        return self._form


# ---------- 1. 登录防爆破 ----------

async def test_login_lockout_after_failures():
    """同IP连续失败达到上限后锁定（err=3），成功登录也无法绕过"""
    panel = _make_panel(admin_password="right-pass")
    form = {"username": "admin", "password": "wrong"}
    for i in range(WebPanel.LOGIN_MAX_FAILURES):
        with pytest.raises(Exception) as ei:
            await panel._action_web_login(FakeReq(form=dict(form)))
        assert "/login?err=1" in ei.value.location
    # 已达上限：即使密码正确也锁定
    with pytest.raises(Exception) as ei:
        await panel._action_web_login(
            FakeReq(form={"username": "admin", "password": "right-pass"}))
    assert "/login?err=3" in ei.value.location


async def test_login_success_clears_failures():
    """失败后成功登录清零计数，不误锁"""
    panel = _make_panel(admin_password="right-pass")
    with pytest.raises(Exception):
        await panel._action_web_login(
            FakeReq(form={"username": "admin", "password": "wrong"}))
    resp = await panel._action_web_login(
        FakeReq(form={"username": "admin", "password": "right-pass"}))
    assert resp.status == 302 and resp.location == "/admin"
    for i in range(WebPanel.LOGIN_MAX_FAILURES):
        with pytest.raises(Exception) as ei:
            await panel._action_web_login(
                FakeReq(form={"username": "admin", "password": "wrong"}))
        assert "/login?err=1" in ei.value.location  # 未被误锁


# ---------- 2. 扫码会话隔离 ----------

async def test_qr_login_session_isolated():
    """他人不能查看/取消我的扫码登录（原全局单会话可窥探二维码）"""
    panel = _make_panel(admin_password="p")
    panel._login_owner = "alice"
    panel._login_state = {"status": "waiting", "message": "扫码",
                          "qr": b"PNGDATA"}

    class AliceReq(FakeReq):
        pass

    alice_sid = panel._create_session("alice", is_admin=False)
    bob_sid = panel._create_session("bob", is_admin=False)

    qr_bob = await panel._handle_login_qr(FakeReq(cookies={"blivepost_sid": bob_sid}))
    assert qr_bob.status == 403

    status_bob = await panel._handle_login_status(
        FakeReq(cookies={"blivepost_sid": bob_sid}))
    assert status_bob.status == 200
    assert status_bob.text == '{"status": "", "message": ""}'  # 空壳不泄露状态

    qr_alice = await panel._handle_login_qr(
        FakeReq(cookies={"blivepost_sid": alice_sid}))
    assert qr_alice.status == 200 and qr_alice.body == b"PNGDATA"


# ---------- 3. web_token ----------

def test_web_token_cookie_no_longer_accepted():
    """长效管理员令牌不再经cookie持久化/接受（防泄漏即永久管理员）"""
    panel = _make_panel(token="SECRET")
    assert panel._web_token_ok(FakeReq(cookies={"web_token": "SECRET"})) is False
    assert panel._web_token_ok(FakeReq(query={"token": "SECRET"})) is True
    assert panel._web_token_ok(FakeReq(headers={"X-Web-Token": "SECRET"})) is True
    assert panel._web_token_ok(FakeReq(query={"token": "WRONG"})) is False


async def test_token_login_does_not_set_persistent_cookie():
    """?token=登录只发会话cookie，不再写web_token长效cookie"""
    panel = _make_panel(token="SECRET")
    resp = await panel._auth_token_to_admin(FakeReq(query={"token": "SECRET"},
                                                    path="/"))
    assert resp is not None
    # 未prepare的aiohttp响应：cookie在resp.cookies（SimpleCookie）
    names = set(resp.cookies.keys())
    assert "blivepost_sid" in names
    assert "web_token" not in names


# ---------- 4. 导出脱敏 ----------

def _make_db_with_credentials(path):
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE cookie_account (uid INTEGER, cookie_string TEXT)")
    con.execute("INSERT INTO cookie_account VALUES (1, 'SESSDATA=secret')")
    con.execute("CREATE TABLE group_binding (room_id INTEGER)")
    con.execute("INSERT INTO group_binding VALUES (23058)")
    con.commit()
    con.close()


def _zip_member_db(buf: io.BytesIO, arcname: str) -> sqlite3.Connection:
    with zipfile.ZipFile(buf) as zf:
        data = zf.read(arcname)
    import tempfile, os
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.write(fd, data)
    os.close(fd)
    return sqlite3.connect(tmp), tmp


def test_export_sanitized_by_default(tmp_path):
    """默认导出为脱敏快照：cookie_account清空、其余数据保留"""
    db_path = tmp_path / "main.db"
    _make_db_with_credentials(db_path)
    buf = WebPanel._build_export_zip(db_path, tmp_path, include_credentials=False)
    con, tmp = _zip_member_db(buf, "data/main.db")
    try:
        creds = con.execute("SELECT COUNT(*) FROM cookie_account").fetchone()[0]
        bindings = con.execute("SELECT COUNT(*) FROM group_binding").fetchone()[0]
    finally:
        con.close()
        import os
        os.unlink(tmp)
    assert creds == 0
    assert bindings == 1


def test_export_raw_with_explicit_flag(tmp_path):
    """?credentials=1 显式导出原始凭证（备份用途）"""
    db_path = tmp_path / "main.db"
    _make_db_with_credentials(db_path)
    buf = WebPanel._build_export_zip(db_path, tmp_path, include_credentials=True)
    con, tmp = _zip_member_db(buf, "data/main.db")
    try:
        creds = con.execute("SELECT COUNT(*) FROM cookie_account").fetchone()[0]
    finally:
        con.close()
        import os
        os.unlink(tmp)
    assert creds == 1


# ---------- 5. /api/status 鉴权 ----------

async def test_api_status_requires_login(monkeypatch):
    """/api/status 含app_id/账号情报，游客401"""
    panel = _make_panel()
    resp = await panel._handle_api(FakeReq())
    assert resp.status == 401

    async def fake_collect():
        return {"ok": True}

    monkeypatch.setattr(panel, "_collect", fake_collect)
    sid = panel._create_session("u1", is_admin=False)
    resp2 = await panel._handle_api(FakeReq(cookies={"blivepost_sid": sid}))
    assert resp2.status == 200


# ---------- 6. 签名会话持久化（进程重启登录态不失效） ----------

async def test_signed_session_survives_restart():
    """配置web_token：新panel实例（同token派生同密钥，模拟重启）旧sid仍有效"""
    import json as _json

    from blivepost.services.webpanel_auth import _b64url_decode

    panel1 = _make_panel(token="SECRET")
    sid = panel1._create_session("alice", is_admin=False)
    # sid为 payload.signature（无状态、自包含）
    assert sid.count(".") == 1
    payload = _json.loads(_b64url_decode(sid.split(".")[0]))
    assert payload["u"] == "alice" and payload["a"] == 0 and "e" in payload

    panel2 = _make_panel(token="SECRET")  # 模拟重启：新实例、同token
    sess = panel2._session(FakeReq(cookies={"blivepost_sid": sid}))
    assert sess is not None
    assert sess["user"] == "alice" and sess["admin"] is False


def test_tampered_sid_rejected():
    """篡改payload（提权管理员）沿用原签名→验签失败变游客；原sid不受影响"""
    import json as _json

    from blivepost.services.webpanel_auth import _b64url_decode, _b64url_encode

    panel = _make_panel(token="SECRET")
    sid = panel._create_session("alice", is_admin=False)
    body, sig = sid.split(".")
    payload = _json.loads(_b64url_decode(body))
    payload["a"] = 1  # 伪造管理员位
    forged = _b64url_encode(_json.dumps(payload).encode("utf-8")) + "." + sig
    assert panel._session_from_sid(forged) is None
    assert panel._session_from_sid(sid) is not None  # 原sid仍有效


def test_expired_sid_rejected():
    """过期会话验签通过但验期失败→游客"""
    import time as _time

    from blivepost.services.webpanel_auth import create_session_sid

    panel = _make_panel(token="SECRET")
    expired = create_session_sid("alice", False, _time.time() - 1,
                                 panel._session_key)
    assert panel._session_from_sid(expired) is None
    assert panel._session(FakeReq(cookies={"blivepost_sid": expired})) is None


def test_random_key_without_web_token():
    """未配置web_token：两个实例密钥不同，旧会话跨实例（重启）失效"""
    panel1 = _make_panel()
    sid = panel1._create_session("alice", is_admin=False)
    assert panel1._session_from_sid(sid) is not None
    panel2 = _make_panel()
    assert panel1._session_key != panel2._session_key
    assert panel2._session_from_sid(sid) is None


def test_web_token_change_invalidates_all_sessions():
    """web_token变更→旧签名全部失效（紧急全量登出手段）"""
    panel1 = _make_panel(token="OLD")
    sid = panel1._create_session("alice", is_admin=True)
    assert panel1._session_from_sid(sid) is not None
    panel2 = _make_panel(token="NEW")
    assert panel2._session_from_sid(sid) is None
