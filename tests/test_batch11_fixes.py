# -*- coding: utf-8 -*-
"""第十一批真机反馈修复回归测试

1. @全体无权限降级：带标签发送失败→去标签重发成功，且会话内不再尝试该群
2. 绑定群编辑器复选框回显勾选
3. 收入相关统一两位小数到分位（无4.499999999999999尾差）
"""

from blivepost.config.settings import Settings
from blivepost.database.manager import DatabaseManager
from blivepost.database.service import DatabaseService
from blivepost.services.push import PushService
from blivepost.services.qqbot import QQBotClient


# ---------- 1. @全体降级 ----------

class _DenyTagBot(QQBotClient):
    """模拟无群管理员权限：含@全体标签的发送被拒，无标签正常"""

    def __init__(self):
        super().__init__("id", "sec")
        self.md = []
        self.text = []

    async def send_group_markdown(self, group, content, keyboard=None):
        self.md.append((group, content))
        return "<qqbot-at-everyone />" not in content

    async def send_group_message(self, group, content, msg_id=None, msg_seq=None):
        self.text.append((group, content))
        return "<qqbot-at-everyone />" not in content


async def test_at_all_degrades_without_permission():
    """无权限群：带标签双链全失败→自动去标签重发成功，消息必达"""
    bot = _DenyTagBot()
    ok = await bot._broadcast_message(["G1"], "开播啦", at_all_groups=["G1"])
    assert ok is True
    # 首轮失败：markdown带标签失败→文本带标签也失败
    assert "<qqbot-at-everyone />" in bot.md[0][1]
    assert "<qqbot-at-everyone />" in bot.text[0][1]
    # 降级重发：markdown无标签成功（直达，无需再走文本）
    assert len(bot.md) == 2 and "<qqbot-at-everyone />" not in bot.md[1][1]
    assert len(bot.text) == 1
    assert "G1" in bot._at_all_unsupported


async def test_at_all_unsupported_not_retried():
    """已确认不支持的群：后续推送直接跳过艾特（不再打失败API）"""
    bot = _DenyTagBot()
    bot._at_all_unsupported.add("G1")
    ok = await bot._broadcast_message(["G1"], "再开播", at_all_groups=["G1"])
    assert ok is True
    assert all("<qqbot-at-everyone />" not in c for _, c in bot.md + bot.text)


async def test_at_all_normal_group_unaffected():
    """有权限群不受降级逻辑影响（标签直达，单次发送）"""
    bot = QQBotClient("id", "sec")
    sent = []

    async def md(group, content, keyboard=None):
        sent.append(content)
        return True

    bot.send_group_markdown = md
    ok = await bot._broadcast_message(["G2"], "开播", at_all_groups=["G2"])
    assert ok is True and sent[0].startswith("<qqbot-at-everyone />")


# ---------- 2. 编辑器回显 ----------

def test_live_editor_checkbox_checked():
    """已绑定群的复选框渲染checked属性"""
    from blivepost.services.webpanel_common import _env
    html = _env.get_template("base.html").render(
        page_template="pages/live.html",
        page="live", nav=[], current_user="u", is_admin=False,
        can_modify=True,
        rooms=[{"room_id": 3060820, "streamer": "洛沐清",
                "push_groups": ["websocket测试"],
                "push_group_openids": ["FA9B838B"],
                "monitored": True, "alive": True}],
        group_options=[("FA9B838B", "websocket测试"), ("B15E4876", "WebSocket_Luo")],
        bindings=[], dynamic_ups=[], dyn_page_ctx={}, switch_badges={},
    )
    import re
    checked = re.findall(r'value="([A-Z0-9]+)"[^>]*checked', html)
    assert "FA9B838B" in checked and "B15E4876" not in checked


# ---------- 3. 收入两位小数 ----------

async def test_revenue_rounded_to_cents(tmp_path):
    """汇总出口两位小数：4499瓜子=4.499元→4.5而非4.499999…；区间/多房合并同理"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    try:
        # 三笔金瓜子：2000+2000+499=4499瓜子 → 4.499元 → 应为4.5（两位舍入）
        for price in (2000, 2000, 499):
            await svc.save_gift(room_id=23058, uid=1, uname="u", gift_id=1,
                                gift_name="辣条", num=1, price=price,
                                coin_type="gold")
        s = await svc.get_daily_summary(23058)
        assert s["gift_gold"] == 4.5
        assert str(s["gift_gold"]) != "4.499999999999999"
        # 多房列表合并也两位
        await svc.save_gift(room_id=23059, uid=1, uname="u", gift_id=1,
                            gift_name="辣条", num=1, price=499,
                            coin_type="gold")
        merged = await svc.get_daily_summary([23058, 23059])
        assert merged["gift_gold"] == 5.0  # 4.499+0.499=4.999→两位舍入5.0
    finally:
        await svc.aclose()
        await db.close()


async def test_income_command_formats_cents():
    """/收入、/汇总 文本到分位（4.5显示为4.50）"""
    from blivepost.services.commands import CommandHandler

    class Db:
        async def get_summary_range(self, rid, start, end):
            return {"day": "x", "danmaku_count": 1, "gift_count": 3,
                    "gift_gold": 4.5, "ship_count": 0, "ship_total": 0.0,
                    "sc_count": 0, "sc_total": 0, "red_pocket": 0}

    class Mgr:
        async def get_room_info(self, rid):
            class Info:
                room_id = 23058
            return Info()

    class Store:
        def get_monitored_room_ids(self):
            return [23058]

        def get_monitored_groups(self, rid):
            return ["G1"]

        def get_streamer_name(self, uid):
            return "测试"

        def get_room_uid(self, rid):
            return 1

    handler = CommandHandler(Store(), Mgr(), db_service=Db())
    text = await handler._income("G1", 23058, None, 7)  # range_days走get_summary_range桩
    assert "4.50元" in text and "0.00元" in text


async def test_recent_live_status_json_serializable(tmp_path):
    """/api/status含下播记录时必须可JSON序列化（end_time原为裸datetime致500）"""
    import json
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    try:
        await svc.save_live_end(room_id=23058, uid=1, title="标题",
                                stats=None) if False else None
        # 直接落一条下播记录（save_live_end签名复杂，走ORM；时间为13位毫秒）
        async with svc._event_session(23058) as session:
            from blivepost.database.models import LiveStatusRecord
            from blivepost.utils.timeutil import now_ms
            session.add(LiveStatusRecord(
                room_id=23058, status="live_end", title="下播了",
                danmaku_count=1, gold_count=0, ship_count=0, sc_count=0,
                duration="01:00:00", end_time=now_ms(), timestamp=now_ms()))
            await session.commit()
        rows = await svc.get_recent_live_status(limit=5)
        assert rows and rows[0]["end_time"]
        json.dumps(rows)  # 裸datetime会在此抛TypeError
    finally:
        await svc.aclose()
        await db.close()


# ---------- 角色门控与配额退还（用户规则：群主/管理员才艾特；失败不计数） ----------

class _Store:
    def get_dynamic_groups(self, up_uid):
        return []

    def get_dynamic_up_name(self, up_uid):
        return ""


class _Ps(PushService):
    """最小PushService桩（bot_client/contact_store可注入）"""

    def __init__(self):
        super().__init__(Settings())
        self.bot_client = None


class _RoleDb:
    """peek/mark两段式配额桩：记录mark调用"""

    def __init__(self, used_dates=None):
        self.used = dict(used_dates or {})
        self.mark_calls = []

    async def acquire_at_all_groups(self, groups, room_id=0, mark=True):
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y-%m-%d")
        eligible = [g for g in groups if self.used.get(g) != today]
        if mark:
            for g in eligible:
                self.used[g] = today
            self.mark_calls.append(list(eligible))
        return eligible

    async def clear_group_at_all(self, group_openid):
        self.used.pop(group_openid, None)
        return True


class _RoleBot:
    """带get_group_role的bot桩（角色可配）；无发送（本组用例只测门控）"""

    def __init__(self, roles):
        self.roles = roles

    async def get_group_role(self, group_openid):
        return self.roles.get(group_openid)


async def test_role_gate_member_skipped_owner_marked():
    """member角色跳过且不占配额；owner/admin放行并登记"""
    from blivepost.core.consumer import PushQueueConsumer

    ps = _Ps()
    ps.bot_client = _RoleBot({"G_OWNER": "owner", "G_ADMIN": "admin",
                              "G_MEMBER": "member"})
    db = _RoleDb()
    consumer = PushQueueConsumer(None, ps, db, binding_store=_Store())
    result = await consumer._acquire_at_all_for_groups(
        ["G_OWNER", "G_ADMIN", "G_MEMBER"])
    assert sorted(result) == ["G_ADMIN", "G_OWNER"]      # member被角色门控跳过
    assert db.mark_calls == [["G_OWNER", "G_ADMIN"]]     # 仅放行群登记配额
    assert "G_MEMBER" not in db.used                     # 未占当日配额


async def test_role_unknown_falls_back_to_attempt():
    """角色查询不可用（None=白名单外/网络失败）→ 放行，靠降级兜底"""
    from blivepost.core.consumer import PushQueueConsumer

    ps = _Ps()
    ps.bot_client = _RoleBot({"G1": None})
    db = _RoleDb()
    consumer = PushQueueConsumer(None, ps, db, binding_store=_Store())
    assert await consumer._acquire_at_all_for_groups(["G1"]) == ["G1"]


async def test_custom_bot_without_role_api_all_allowed():
    """自定义bot无get_group_role：全部放行（duck-type兼容）"""
    from blivepost.core.consumer import PushQueueConsumer

    ps = _Ps()
    ps.bot_client = object()  # 无get_group_role属性
    db = _RoleDb()
    consumer = PushQueueConsumer(None, ps, db, binding_store=_Store())
    assert await consumer._acquire_at_all_for_groups(["G1", "G2"]) == ["G1", "G2"]


async def test_at_all_failure_refunds_quota():
    """发送失败退还配额：降级路径调用at_all_refund，当日可再次艾特"""
    bot = _DenyTagBot()
    refunded = []

    async def _refund(group):
        refunded.append(group)

    bot.at_all_refund = _refund
    ok = await bot._broadcast_message(["G9"], "开播", at_all_groups=["G9"])
    assert ok is True
    assert refunded == ["G9"]                      # 失败即退还
    assert "G9" in bot._at_all_unsupported


async def test_at_all_success_keeps_quota():
    """发送成功不退还（配额正常占用）"""
    bot = QQBotClient("id", "sec")
    refunded = []

    async def md(group, content, keyboard=None):
        return True

    bot.send_group_markdown = md
    bot.at_all_refund = lambda g: refunded.append(g)
    await bot._broadcast_message(["GK"], "开播", at_all_groups=["GK"])
    assert refunded == []


async def test_acquire_peek_does_not_mark(tmp_path):
    """mark=False纯查询不写库（配额未消耗）；随后mark=True登记"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    try:
        peek1 = await svc.acquire_at_all_groups(["GA"], mark=False)
        peek2 = await svc.acquire_at_all_groups(["GA"], mark=False)
        assert peek1 == ["GA"] == peek2            # 两次peek均可用（未写入）
        marked = await svc.acquire_at_all_groups(["GA"])   # mark=True
        assert marked == ["GA"]
        after = await svc.acquire_at_all_groups(["GA"], mark=False)
        assert after == []                          # 已占用
        await svc.clear_group_at_all("GA")          # 退还后恢复
        assert await svc.acquire_at_all_groups(["GA"], mark=False) == ["GA"]
    finally:
        await svc.aclose()
        await db.close()


async def test_get_group_role_parsing_and_cache():
    """bot_state响应解析（平铺/包裹两形态）+ TTL缓存 + 失败返回None"""
    bot = QQBotClient("id", "sec")
    calls = []

    class _Http:
        async def request(self, route):
            calls.append(route)
            return {"member_openid": "X", "member_role": "admin"}

    class _Api:
        _http = _Http()

    class _Client:
        api = _Api()

    bot._client = _Client()
    import asyncio as _aio
    bot._task = _aio.get_running_loop().create_task(_aio.sleep(30))
    assert await bot.get_group_role("G1") == "admin"
    assert await bot.get_group_role("G1") == "admin"   # 第二次命中缓存
    assert len(calls) == 1
    assert "/v2/groups/" in calls[0].path
    bot._group_role_cache.clear()

    async def failing(route):
        raise RuntimeError("11253 应用无接口访问权限")

    _Http.request = failing
    assert await bot.get_group_role("G2") is None      # 白名单外→None→放行兜底
    bot._task.cancel()


# ---------- token建会话与已移出群过滤（第十一批反馈二） ----------

async def test_admin_token_login_establishes_session():
    """/admin?token= → 先建会话再重定向（原来仅渲染无sid，页头仍是游客）"""
    from blivepost.services.webpanel import WebPanel
    panel = WebPanel(None, None, None, None, None, token="SECRET",
                     admin_password="p")
    resp = await panel._handle_admin_page(FakeReq(query={"token": "SECRET"},
                                                  path="/admin"))
    # 应为302重定向（去token干净URL）并携带会话cookie，而非直接200渲染
    assert resp.status == 302
    assert "blivepost_sid" in resp.cookies
    sid = resp.cookies["blivepost_sid"].value
    sess = panel._session_from_sid(sid)
    assert sess is not None and sess["admin"] is True


class FakeReq:
    def __init__(self, query=None, headers=None, cookies=None, form=None, path="/"):
        self.query = query or {}
        self.headers = headers or {}
        self.cookies = cookies or {}
        self._form = form or {}
        self.path = path
        self.remote = "127.0.0.1"


async def test_screen_token_login_establishes_session(monkeypatch):
    """/screen?token= → 建会话重定向（对齐页面管线）"""
    from blivepost.services.webpanel import WebPanel
    panel = WebPanel(None, None, None, None, None, token="SECRET")

    async def fake_ctx():
        return {}

    monkeypatch.setattr(panel, "_screen_context", fake_ctx)
    resp = await panel._handle_screen_page(FakeReq(query={"token": "SECRET"},
                                                   path="/screen"))
    assert resp.status == 302 and "blivepost_sid" in resp.cookies


async def test_load_groups_active_only_filters_removed(tmp_path):
    """已移出群(del_flag=1)不出现在active_only列表与信息回填目标"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    try:
        await svc.save_group("G_KEEP", name="在群")
        await svc.save_group("G_GONE", name="已移出", del_flag=1, receive=0)
        active = await svc.load_groups(active_only=True)
        assert [g.group_openid for g in active] == ["G_KEEP"]
        assert len(await svc.load_groups()) == 2      # 不过滤时全量（联系人store用）
        missing = await svc.list_groups_missing_info()
        assert "G_GONE" not in missing                 # 移出群不再回填
    finally:
        await svc.aclose()
        await db.close()


# ---------- 按钮keyboard透传 + 告警回退（第十一批反馈三） ----------

async def test_official_bot_media_path_passes_keyboard():
    """官方bot主路径：push_media/push 必须透传keyboard（原丢失致按钮不渲染）"""
    ps = PushService(Settings())

    class RoomStore:
        def get_monitored_groups(self, room_id):
            return ["G1"]

    ps.binding_store = RoomStore()

    class Bot:
        def __init__(self):
            self.media_calls = []
            self.push_calls = []

        def push_media(self, image, content, groups, keyboard=None,
                       at_all_groups=None):
            self.media_calls.append((keyboard, at_all_groups))
            return True

        def push(self, content, groups, keyboard=None, media=None,
                 at_all_groups=None):
            self.push_calls.append((keyboard, at_all_groups))
            return True

    ps.bot_client = Bot()
    kb = {"content": {"rows": [{"buttons": [{"id": "enter"}]}]}}
    ps.push_live_start_from_parts("开播", "http://c", 23058, kb,
                                  at_all_groups=["G1"])
    assert ps.bot_client.media_calls[0][0] is kb        # keyboard透传（按钮渲染前提）
    assert ps.bot_client.media_calls[0][1] == ["G1"]
    # 无图路径同样透传
    ps.push_live_start_from_parts("开播", None, 23058, kb)
    assert ps.bot_client.push_calls[0][0] is kb


async def test_notify_admins_falls_back_to_main():
    """openids未配置时告警回退主管理员（原仅落日志）"""
    from blivepost.core.consumer import PushQueueConsumer

    class NotifyBot:
        def __init__(self):
            self.sent = []

        async def notify_admins(self, targets, content):
            self.sent.append(list(targets))
            return 1

    ps = _Ps()
    ps.bot_client = NotifyBot()
    consumer = PushQueueConsumer(None, ps, None,
                                 admin_openids=[], main_admin_openids=["MAIN1"])
    await consumer._notify_admins("普通告警", sensitive=False)
    assert ps.bot_client.sent == [["MAIN1"]]
