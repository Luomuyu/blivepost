# -*- coding: utf-8 -*-
"""第十三批：@全体配置化 + 指令体验测试

1. at_all三模式：daily默认/rooms仅指定房间/off关闭
2. /艾特全体 指令：查看/每日/仅/关闭
3. 未知指令模糊提示
"""

from blivepost.config.settings import Settings
from blivepost.database.manager import DatabaseManager
from blivepost.database.service import DatabaseService


async def test_at_all_mode_three_forms(tmp_path):
    """at_all三模式：off跳过、rooms按房间过滤、daily默认"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    try:
        # 无行=daily默认：首次可艾特
        r1 = await svc.acquire_at_all_groups(["GA"], room_id=100, mark=True)
        assert r1 == ["GA"]
        # 设off → 跳过
        await svc.set_group_at_all_mode("GB", "off")
        r2 = await svc.acquire_at_all_groups(["GA", "GB"], room_id=100, mark=False)
        assert "GB" not in r2           # off群不参与
        # 设rooms=200,300 → room_id=100时不参与，=200时参与
        await svc.set_group_at_all_mode("GC", "rooms", "200,300")
        r3 = await svc.acquire_at_all_groups(["GC"], room_id=100, mark=False)
        assert r3 == []                 # 100不在允许列表
        r4 = await svc.acquire_at_all_groups(["GC"], room_id=200, mark=False)
        assert r4 == ["GC"]             # 200在允许列表
        # 配置查询
        cfg = await svc.get_group_at_all_mode("GC")
        assert cfg["mode"] == "rooms" and "200" in cfg["room_ids"]
        cfg2 = await svc.get_group_at_all_mode("GNEW")
        assert cfg2["mode"] == "daily"  # 无行=daily
        # 非法模式 → False
        assert await svc.set_group_at_all_mode("GX", "bad") is False
    finally:
        await svc.aclose()
        await db.close()


async def test_at_all_command_forms():
    """/艾特全体 指令四形态"""
    from blivepost.services.commands import CommandHandler

    class Db:
        def __init__(self):
            self.mode_calls = []

        async def get_group_at_all_mode(self, group):
            return {"mode": "daily", "room_ids": ""}

        async def set_group_at_all_mode(self, group, mode, room_ids=""):
            self.mode_calls.append((mode, room_ids))
            return True

    class Store:
        def get_group_bindings(self, group):
            return []

        def get_group_dynamic_ups(self, group):
            return []

    db = Store(), Db()
    ch = CommandHandler(db[0], None, db_service=db[1])

    # 查看
    reply = await ch._at_all_mode("G1", "查看")
    assert "每日首次" in reply and "艾特全体" in reply
    # 每日
    reply = await ch._at_all_mode("G1", "每日")
    assert "每日" in reply and db[1].mode_calls[-1][0] == "daily"
    # 关闭
    reply = await ch._at_all_mode("G1", "关闭")
    assert "已关闭" in reply and db[1].mode_calls[-1][0] == "off"
    # 仅指定房间
    reply = await ch._at_all_mode("G1", "仅 100,200")
    assert "100,200" in reply and db[1].mode_calls[-1][0] == "rooms"
    assert db[1].mode_calls[-1][1] == "100,200"


def test_fuzzy_command_suggestion():
    """未知指令模糊提示：/监空→/监控；非/开头不触发；无近似→None"""
    from blivepost.services.commands import CommandHandler

    assert CommandHandler._suggest_command("/监空 23058") == \
        "你是不是想用 /监控？发送 /帮助 查看全部指令"
    assert CommandHandler._suggest_command("/取消 23058") is not None
    assert CommandHandler._suggest_command("你好") is None   # 非/开头
    assert CommandHandler._suggest_command("/zzzzz") is None  # 无近似
    # /帮助在已知列表中，handle由_NOARG_RE处理不走suggest；但difflib仍会匹配，属预期行为


def test_help_text_grouped():
    """/帮助 按权限分组（查询/控制）"""
    from blivepost.services.commands import HELP_TEXT
    assert "【查询" in HELP_TEXT and "【控制" in HELP_TEXT
    assert "/艾特全体" in HELP_TEXT
