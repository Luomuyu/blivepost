# -*- coding: utf-8 -*-
"""第十一批真机反馈修复回归测试

1. @全体无权限降级：带标签发送失败→去标签重发成功，且会话内不再尝试该群
2. 绑定群编辑器复选框回显勾选
3. 收入相关统一两位小数到分位（无4.499999999999999尾差）
"""

from blivepost.config.settings import Settings
from blivepost.database.manager import DatabaseManager
from blivepost.database.service import DatabaseService
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
        # 直接落一条下播记录（save_live_end签名复杂，走ORM）
        async with svc._event_session(23058) as session:
            from blivepost.database.models import LiveStatusRecord
            from datetime import datetime as _dt
            session.add(LiveStatusRecord(
                room_id=23058, status="live_end", title="下播了",
                danmaku_count=1, gold_count=0, ship_count=0, sc_count=0,
                duration="01:00:00", end_time=_dt.now(), timestamp=_dt.now()))
            await session.commit()
        rows = await svc.get_recent_live_status(limit=5)
        assert rows and rows[0]["end_time"]
        json.dumps(rows)  # 裸datetime会在此抛TypeError
    finally:
        await svc.aclose()
        await db.close()
