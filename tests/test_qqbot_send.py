# -*- coding: utf-8 -*-
"""QQ官方机器人发送链路测试（src/blivepost/services/qqbot.py）

botpy的HTTP层全部以duck-type桩替换（记录调用、按剧本抛错），不打真网络，
也不实例化_PushClient（其构造依赖botpy且可能拉起websocket）。started状态
用假task（asyncio.sleep）模拟——参考tests/test_stage1_fixes.py的做法。

覆盖：
1. send_group_message：正常发送/未启动守卫/失败False/带按钮去按钮重发/
   40034105主动消息拒绝走低频提示
2. send_group_media：post_group_file+msg_type=7/带按钮去按钮重发/未启动守卫
3. push/broadcast/push_media兼容入口：委托关系、未启动False、worker出错
   续跑、队列满丢弃时关闭被丢协程
4. notify_admins / send_c2c_message / send_c2c_image 正例
5. _broadcast_message：Markdown无权限降级纯文本（图片URL保留）
"""

import asyncio
import inspect
import logging
import time
from contextlib import asynccontextmanager, contextmanager, suppress
from types import SimpleNamespace

from blivepost.services.qqbot import QQBotClient


# ---------- 桩与工具 ----------

class _FakeApi:
    """botpy client.api层的记录桩：按调用顺序记录kwargs，可按剧本抛错"""

    def __init__(self):
        self.calls = []
        # 剧本：方法名 -> 异常列表（每次调用弹出队首；None表示本次放行）
        self.fail_plan = {}
        # 命中这些msg_type的post_group_message总是抛错（Markdown降级用）
        self.fail_msg_types = set()

    def _maybe_raise(self, method, kwargs=None):
        plan = self.fail_plan.get(method)
        if plan:
            err = plan.pop(0)
            if err is not None:
                raise err
        if kwargs is not None and kwargs.get("msg_type") in self.fail_msg_types:
            raise RuntimeError(f"msg_type={kwargs['msg_type']} 不被允许（测试剧本）")

    async def post_group_message(self, **kwargs):
        self.calls.append(("post_group_message", kwargs))  # 先记录：失败调用也入账
        self._maybe_raise("post_group_message", kwargs)

    async def post_group_file(self, **kwargs):
        self.calls.append(("post_group_file", kwargs))
        self._maybe_raise("post_group_file")
        return "MEDIA_TOKEN"

    async def post_c2c_message(self, **kwargs):
        self.calls.append(("post_c2c_message", kwargs))
        self._maybe_raise("post_c2c_message")

    def method_calls(self, method):
        """取指定方法的全部调用kwargs列表"""
        return [kw for m, kw in self.calls if m == method]


@asynccontextmanager
async def _running_bot(api=None):
    """构造started=True的QQBotClient：挂桩api与假task，退出时统一收尾"""
    bot = QQBotClient("test-app", "test-secret")
    bot._loop = asyncio.get_running_loop()
    bot._task = asyncio.create_task(asyncio.sleep(30))  # 使started为True
    if api is not None:
        bot._client = SimpleNamespace(api=api)  # 只替换api，不建_PushClient
    try:
        yield bot
    finally:
        if bot._send_workers:
            for w in bot._send_workers:
                w.cancel()
            await asyncio.gather(*bot._send_workers, return_exceptions=True)
        bot._task.cancel()
        with suppress(asyncio.CancelledError):
            await bot._task


class _WarningCollector(logging.Handler):
    """收集blivepost.qqbot日志（父logger propagate=False，caplog收不到）"""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@contextmanager
def _capture_qqbot_warnings():
    collector = _WarningCollector()
    lg = logging.getLogger("blivepost.qqbot")
    lg.addHandler(collector)
    try:
        yield collector.messages
    finally:
        lg.removeHandler(collector)


async def _wait_until(predicate, timeout=3.0):
    """轮询等待条件成立（沿用现有测试的for/sleep轮询风格）"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# ---------- 1. send_group_message ----------

async def test_send_group_message_ok():
    """正常发送：msg_type=0文本、群号/内容/keyboard透传，返回True"""
    api = _FakeApi()
    async with _running_bot(api) as bot:
        kb = {"content": {"rows": []}}
        assert await bot.send_group_message("G1", "开播啦", keyboard=kb) is True
        assert api.method_calls("post_group_message") == [
            {"group_openid": "G1", "msg_type": 0, "content": "开播啦", "keyboard": kb}
        ]


async def test_send_group_message_not_started():
    """客户端未运行：直接返回False且不触碰api层"""
    bot = QQBotClient("test-app", "test-secret")
    assert await bot.send_group_message("G1", "hello") is False


async def test_send_group_message_failure_returns_false():
    """接口异常且无按钮可降级：返回False"""
    api = _FakeApi()
    api.fail_plan = {"post_group_message": [RuntimeError("服务器开小差")]}
    async with _running_bot(api) as bot:
        assert await bot.send_group_message("G1", "hello") is False
        assert len(api.method_calls("post_group_message")) == 1  # 无按钮不重发


async def test_send_group_message_keyboard_retry():
    """带按钮发送失败：自动去按钮重发一次并成功"""
    api = _FakeApi()
    api.fail_plan = {"post_group_message": [RuntimeError("keyboard未授权")]}
    async with _running_bot(api) as bot:
        kb = {"content": {"rows": []}}
        assert await bot.send_group_message("G1", "带按钮", keyboard=kb) is True
        calls = api.method_calls("post_group_message")
        assert len(calls) == 2
        assert calls[0]["keyboard"] == kb  # 首次带按钮
        assert calls[1]["keyboard"] is None  # 重发去按钮


async def test_send_group_message_denied_40034105_lowfreq():
    """主动消息40034105拒绝：返回False并登记群号；6小时内重复拒绝仅提示一次"""
    api = _FakeApi()
    denied = RuntimeError("错误码: 40034105 主动消息失败, 无权限")
    api.fail_plan = {"post_group_message": [denied, RuntimeError("40034105 再拒")]}
    async with _running_bot(api) as bot:
        with _capture_qqbot_warnings() as messages:
            assert await bot.send_group_message("G1", "推送") is False
            assert await bot.send_group_message("G1", "推送") is False
        assert "G1" in bot._deny_warned  # 命中主动消息权限拒绝路径
        warned = [m for m in messages if "主动消息被平台拒绝" in m]
        assert len(warned) == 1  # 低频：第二次拒绝不再刷屏


def test_note_send_denied_unit():
    """_note_send_denied单元：不匹配不登记；匹配登记；窗口内不重复提示、过期重新提示"""
    bot = QQBotClient("test-app", "test-secret")
    # 不匹配的错误：不算主动消息拒绝
    assert bot._note_send_denied("G9", RuntimeError("其他错误")) is False
    assert "G9" not in bot._deny_warned
    with _capture_qqbot_warnings() as messages:
        assert bot._note_send_denied("G9", RuntimeError("40034105 无权限")) is True
        assert "G9" in bot._deny_warned
        # 窗口内重复命中：仍返回True但不重复提示
        assert bot._note_send_denied("G9", RuntimeError("主动消息失败")) is True
        assert len([m for m in messages if "主动消息被平台拒绝" in m]) == 1
        # 快进7小时（超6小时窗口）：重新提示并刷新时间戳
        old_ts = bot._deny_warned["G9"]
        bot._deny_warned["G9"] = time.monotonic() - 7 * 3600
        assert bot._note_send_denied("G9", RuntimeError("40034105")) is True
        assert bot._deny_warned["G9"] > old_ts
        assert len([m for m in messages if "主动消息被平台拒绝" in m]) == 2


# ---------- 2. send_group_media ----------

async def test_send_group_media_ok():
    """正常富媒体：先post_group_file(file_type=1, url)再msg_type=7带media"""
    api = _FakeApi()
    async with _running_bot(api) as bot:
        assert await bot.send_group_media(
            "G1", "http://img/a.png", content="封面来了") is True
        assert api.method_calls("post_group_file") == [
            {"group_openid": "G1", "file_type": 1, "url": "http://img/a.png"}
        ]
        assert api.method_calls("post_group_message") == [
            {"group_openid": "G1", "msg_type": 7, "content": "封面来了",
             "media": "MEDIA_TOKEN", "keyboard": None}
        ]


async def test_send_group_media_keyboard_retry():
    """带按钮富媒体失败：去按钮重发（文件重新上传一次）"""
    api = _FakeApi()
    api.fail_plan = {"post_group_message": [RuntimeError("富媒体按钮拒绝")]}
    async with _running_bot(api) as bot:
        kb = {"content": {"rows": []}}
        assert await bot.send_group_media(
            "G1", "http://img/b.png", content="配图", keyboard=kb) is True
        assert len(api.method_calls("post_group_file")) == 2  # 每次发送各上传一次
        calls = api.method_calls("post_group_message")
        assert calls[0]["keyboard"] == kb and calls[1]["keyboard"] is None


async def test_send_group_media_not_started():
    """客户端未运行：富媒体发送直接False"""
    bot = QQBotClient("test-app", "test-secret")
    assert await bot.send_group_media("G1", "http://img/a.png") is False


# ---------- 3. push/broadcast/push_media 兼容入口 ----------

async def test_push_before_start_returns_false():
    """未启动时push/broadcast/push_media统一返回False"""
    bot = QQBotClient("test-app", "test-secret")
    assert bot.push("text", ["G1"]) is False
    assert bot.broadcast(["G1"], "text") is False
    assert bot.push_media("http://img/a.png", "text", ["G1"]) is False


async def test_push_media_and_broadcast_delegate():
    """兼容入口委托关系：push_media携带media、broadcast不携带media"""
    bot = QQBotClient("test-app", "test-secret")
    recorded = []

    def fake_push(content, groups, keyboard=None, media=None, at_all_groups=None):
        recorded.append((content, tuple(groups), keyboard, media))
        return True

    bot.push = fake_push
    assert bot.push_media("http://img/a.png", "配图推送", ["G1", "G2"]) is True
    assert recorded == [("配图推送", ("G1", "G2"), None, "http://img/a.png")]
    kb = {"content": {"rows": []}}
    assert bot.broadcast(["G3"], "纯文本", keyboard=kb) is True
    assert recorded[-1] == ("纯文本", ("G3",), kb, None)


async def test_send_worker_survives_error():
    """worker消费：单条任务抛错不中断worker，后续任务照常执行且队列计数归零"""
    async with _running_bot() as bot:
        bot._send_queue = asyncio.Queue(maxsize=10)
        done = []

        async def boom():
            raise RuntimeError("单条推送失败")

        async def ok():
            done.append("ok")

        bot._send_workers = [asyncio.create_task(bot._send_worker())]
        assert bot._enqueue_send(boom()) is True
        assert bot._enqueue_send(ok()) is True
        assert await _wait_until(lambda: bool(done)) is True
        await asyncio.wait_for(bot._send_queue.join(), timeout=3)
        assert done == ["ok"]


async def test_queue_full_closes_dropped_coroutine():
    """队列满丢弃：返回False且被丢协程已close（不产生never-awaited告警）"""
    async with _running_bot() as bot:
        bot._send_queue = asyncio.Queue(maxsize=1)  # 不启worker：保持满载
        created = []

        def broadcast_factory(groups, content, media=None, keyboard=None,
                              at_all_groups=None):
            async def _job():
                pass  # 永不执行：入队失败即被丢弃
            coro = _job()
            created.append(coro)
            return coro

        bot._broadcast_message = broadcast_factory
        assert bot.push("first", ["G1"]) is True
        assert bot.push("overflow", ["G1"]) is False  # 满则丢弃
        assert len(created) == 2
        assert inspect.getcoroutinestate(created[0]) == inspect.CORO_CREATED
        # 丢弃时同步关闭被丢协程（不产生never-awaited告警）
        assert inspect.getcoroutinestate(created[1]) == inspect.CORO_CLOSED
        created[0].close()  # 收尾：关闭滞留队列的协程，避免本测试自身告警


async def test_broadcast_message_markdown_fallback():
    """_broadcast_message降级链：Markdown(msg_type=2)被拒后降级纯文本，图片以URL保留"""
    api = _FakeApi()
    api.fail_msg_types = {2}  # 模拟群Markdown无权限
    async with _running_bot(api) as bot:
        assert await bot._broadcast_message(
            ["G1"], "开播啦", media="http://img/a.png") is True
        calls = api.method_calls("post_group_message")
        assert len(calls) == 2
        assert calls[0]["msg_type"] == 2          # Markdown优先
        assert "![封面" in calls[0]["markdown"]["content"]  # 原生语法嵌图
        assert calls[1]["msg_type"] == 0          # 降级纯文本
        assert calls[1]["content"] == "开播啦\n图片：http://img/a.png"


# ---------- 4. notify_admins / send_c2c_message / send_c2c_image ----------

async def test_notify_admins_counts_successes():
    """notify_admins逐个私聊：仅统计成功的管理员数，内容与被动参数透传"""
    api = _FakeApi()
    api.fail_plan = {"post_c2c_message": [None, RuntimeError("第二个失败")]}
    async with _running_bot(api) as bot:
        sent = await bot.notify_admins(["A1", "A2"], "Cookie已失效")
        assert sent == 1
        calls = api.method_calls("post_c2c_message")
        assert [kw["openid"] for kw in calls] == ["A1", "A2"]
        assert all(kw["msg_type"] == 0 and kw["content"] == "Cookie已失效"
                   for kw in calls)


async def test_notify_admins_string_defense():
    """误传单个openid字符串：按单元素列表处理而非逐字符发送"""
    api = _FakeApi()
    async with _running_bot(api) as bot:
        assert await bot.notify_admins("ONLY", "提醒") == 1
        calls = api.method_calls("post_c2c_message")
        assert len(calls) == 1 and calls[0]["openid"] == "ONLY"


async def test_send_c2c_message_ok_and_fail():
    """私聊文本：msg_id/msg_seq透传成功；接口失败返回False"""
    api = _FakeApi()
    async with _running_bot(api) as bot:
        assert await bot.send_c2c_message(
            "U1", "你好", msg_id="MID", msg_seq=3) is True
        assert api.method_calls("post_c2c_message")[0] == {
            "openid": "U1", "msg_type": 0, "content": "你好",
            "msg_id": "MID", "msg_seq": 3,
        }
        # 默认参数：主动消息（无msg_id，msg_seq=1）
        assert await bot.send_c2c_message("U2", "主动") is True
        assert api.method_calls("post_c2c_message")[1] == {
            "openid": "U2", "msg_type": 0, "content": "主动",
            "msg_id": None, "msg_seq": 1,
        }
        api.fail_plan = {"post_c2c_message": [RuntimeError("c2c失败")]}
        assert await bot.send_c2c_message("U1", "again") is False


async def test_send_c2c_image_ok_and_upload_fail(monkeypatch):
    """私聊图片：上传得到media后以msg_type=7发送；上传失败直接False不再发消息"""
    api = _FakeApi()
    async with _running_bot(api) as bot:
        uploads = []

        async def fake_upload(openid, image_bytes, filename):
            uploads.append((openid, len(image_bytes), filename))
            return "MEDIA_TOKEN"

        async def none_upload(openid, image_bytes, filename):
            return None

        monkeypatch.setattr(bot, "_upload_image_file", fake_upload)
        assert await bot.send_c2c_image(
            "U1", b"PNGDATA", msg_id="MID", msg_seq=2, filename="qr.png") is True
        assert uploads == [("U1", 7, "qr.png")]
        assert api.method_calls("post_c2c_message") == [
            {"openid": "U1", "msg_type": 7, "content": "", "media": "MEDIA_TOKEN",
             "msg_id": "MID", "msg_seq": 2}
        ]
        # 上传失败：不再调用post_c2c_message
        monkeypatch.setattr(bot, "_upload_image_file", none_upload)
        assert await bot.send_c2c_image("U1", b"x") is False
        assert len(api.method_calls("post_c2c_message")) == 1


def test_enqueue_send_rejects_after_stop_window():
    """stop收尾窗口（队列已置空）：入队拒绝返回False不抛AssertionError"""
    bot = QQBotClient("id", "sec")

    async def _job():
        return True

    coro = _job()
    bot._send_queue = None
    assert bot._enqueue_send(coro) is False
    import inspect
    assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
