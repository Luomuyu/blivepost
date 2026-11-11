# -*- coding: utf-8 -*-
"""第六十一批：弹幕词云（分词统计/范围解析/房间扇出/页面渲染）"""

from datetime import datetime, timedelta

import pytest

from blivepost.config.settings import Settings
from blivepost.database import DatabaseManager, DatabaseService
from blivepost.services.wordcloud import build_wordcloud
from blivepost.services.webpanel import WebPanel, _parse_day
from blivepost.utils.timeutil import dt_to_ms, now_local


# ==================== 分词统计 ====================

def test_build_wordcloud_counts_and_noise():
    """高频词统计：停用词/单字/纯数字/URL被过滤（主播/直播间/哈系为弹幕噪音）"""
    texts = ["主播威武主播霸气", "前排围观前排吃瓜", "23333",
             "关注主播关注直播间", "https://example.com/x"]
    words = {w["word"]: w["count"] for w in build_wordcloud(texts, top_n=50)}
    assert "威武" in words and "霸气" in words
    assert "围观" in words or "吃瓜" in words
    # 停用词/噪音不入榜
    assert "主播" not in words      # 弹幕噪音停用词
    assert "直播间" not in words    # 弹幕噪音停用词
    assert "23333" not in words     # 纯数字
    assert "https" not in words     # URL


def test_build_wordcloud_empty():
    assert build_wordcloud([]) == []
    assert build_wordcloud(["", "   "]) == []


def test_parse_day():
    d = _parse_day("2026-09-11")
    assert (d.year, d.month, d.day) == (2026, 9, 11)
    assert _parse_day("bad") is None
    assert _parse_day("") is None


# ==================== 范围解析与聚合 ====================

@pytest.fixture
async def env(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    panel = WebPanel(None, None, None, None, svc, bot_client=None)
    yield svc, panel, db
    await db.close()


async def _seed(svc, room_id, msg, day: datetime):
    """直接插danmaku行（save_danmaku固定取当前时间，范围测试需指定时间戳；
    时间列存13位毫秒，datetime入参经dt_to_ms换算）"""
    from blivepost.database.models import DanmakuRecord
    async with svc._event_session(room_id) as session:
        session.add(DanmakuRecord(room_id=room_id, uid=1, uname="u", msg=msg,
                                  timestamp=dt_to_ms(day.replace(
                                      hour=12, minute=0, second=0,
                                      microsecond=0))))
        await session.commit()


@pytest.mark.asyncio
async def test_wordcloud_ranges(env):
    """按天/区间/月/年范围解析与统计口径"""
    svc, panel, _db = env
    today = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    await _seed(svc, 100, "主播威武主播霸气", today)
    await _seed(svc, 100, "主播牛", today - timedelta(days=1))
    await _seed(svc, 100, "昨天内容", today - timedelta(days=1))
    await _seed(svc, 100, "本月早期", today.replace(day=1))
    await _seed(svc, 100, "今年年初", today.replace(month=1, day=1))

    ctx = await panel._wordcloud_context("today")
    assert ctx["label"] == "今天"
    assert ctx["total"] == 1  # 今天只种了1条
    words = {w["word"] for w in ctx["words"]}
    assert "威武" in words and "霸气" in words

    ctx_y = await panel._wordcloud_context("yesterday")
    assert ctx_y["total"] == 2  # 昨天2条

    ctx_d3 = await panel._wordcloud_context("d3")
    assert ctx_d3["label"] == "近3天"
    assert ctx_d3["total"] == 3
    words3 = {w["word"] for w in ctx_d3["words"]}
    assert "威武" in words3

    ctx_m = await panel._wordcloud_context("month")
    words_m = {w["word"] for w in ctx_m["words"]}
    assert ctx_m["total"] == 4  # 本月含今天/昨天/月初
    assert "早期" in words_m

    ctx_y = await panel._wordcloud_context("year")
    assert ctx_y["total"] == 5
    words_yr = {w["word"] for w in ctx_y["words"]}
    assert "早期" in words_yr  # 本月月初的弹幕也在年范围内


@pytest.mark.asyncio
async def test_wordcloud_room_scope(env):
    """全部直播间 vs 单独直播间切换"""
    svc, panel, _db = env
    today = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    await _seed(svc, 100, "甲房间专属词汇", today)
    await _seed(svc, 200, "乙房间独家暗号", today)

    ctx_all = await panel._wordcloud_context("today")
    assert ctx_all["total"] == 2
    words_all = {w["word"] for w in ctx_all["words"]}
    assert "暗号" in words_all  # 全部直播间含房间200词汇

    ctx_100 = await panel._wordcloud_context("today", room_id=100)
    assert ctx_100["total"] == 1
    assert ctx_100["label"].endswith("房间100")
    words_100 = {w["word"] for w in ctx_100["words"]}
    assert "暗号" not in words_100  # 房间200的弹幕不串


@pytest.mark.asyncio
async def test_wordcloud_page_and_partial_render(env):
    """页面注册/局部渲染/导航含词云"""
    svc, panel, _db = env
    assert "/wordcloud" in [p for p, _ in WebPanel.PAGES.values()]

    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request("GET", "/wordcloud")
    resp = await panel._make_page_handler("wordcloud")(req)
    assert resp.status == 200
    body = resp.text
    assert "弹幕词云" in body and "renderWordCloud" in body
    assert "词云" in body

    req2 = make_mocked_request("GET", "/panel/wordcloud?range=today")
    resp2 = await panel._handle_wordcloud(req2)
    assert resp2.status == 200
    assert "wordcloud-panel" in resp2.text
