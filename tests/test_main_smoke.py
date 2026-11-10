# -*- coding: utf-8 -*-
"""__main__.py装配层冒烟测试（不真运行主循环）

parse_args参数解析 + main_async的finally清理路径：
- 全部重量组件（Settings.load/DatabaseManager/BindingStore/CookiePool/
  ContactStore/PushService/QQBotClient/MonitorManager/DynamicManager/WebPanel/
  CookieMaintainer/StreamerRefresher/GroupInfoRefresher/PushQueueConsumer/
  DailyReportJob/LiveStateWatcher等）替换为记录桩；
- _listen_terminal替换成立即置位stop_event的桩（不等真实stdin）；
- 验证启动中途抛异常时finally不产生NameError（组件变量预清None回归）、
  已启动组件的stop按既定顺序全部调用、以及sys.exit语义。
"""

import sys

import pytest

from blivepost import __main__ as bm
from blivepost.config.settings import Settings


# ---------- 桩组件 ----------

class _Rec:
    """共享调用记录器：按"组件名.方法"登记调用顺序"""

    def __init__(self):
        self.calls = []

    def __call__(self, sig):
        self.calls.append(sig)

    def stop_calls(self):
        """过滤出全部关闭类调用（stop/stop_all/aclose/close）"""
        tails = ("stop", "stop_all", "aclose", "close")
        return [c for c in self.calls if c.rsplit(".", 1)[-1] in tails]


class _LoggerStub:
    """Logger桩：不写日志文件、不加控制台handler"""

    def setup(self, *args, **kwargs):
        return None

    def apply_debug(self, *args, **kwargs):
        return None


class _DbManagerStub:
    def __init__(self, rec, cfg):
        self.rec = rec

    async def initialize(self):
        self.rec("db_manager.initialize")

    async def close(self):
        self.rec("db_manager.close")


class _DbServiceStub:
    def __init__(self, rec, db_manager):
        self.rec = rec

    async def migrate_legacy_events(self):
        return 0

    async def clear_group_at_all(self, group_openid):
        return True

    async def seed_push_switches(self, defaults):
        return None

    async def get_push_switches(self):
        return {}

    async def seed_group_push_settings(self, room_id, groups, defaults):
        return None

    async def aclose(self):
        self.rec("db_service.aclose")


class _BindingStoreStub:
    def __init__(self, rec, db_service):
        self.rec = rec

    async def load(self):
        self.rec("binding_store.load")

    def get_monitored_room_ids(self):
        return [23058]

    def get_monitored_groups(self, room_id):
        return ["G1"]

    def get_dynamic_up_uids(self):
        return []


class _LoadOnlyStoreStub:
    """CookiePool/ContactStore：只需异步load"""

    def __init__(self, rec, name, db_service):
        self.rec = rec
        self.name = name

    async def load(self):
        self.rec(f"{self.name}.load")


class _PlainStub:
    """无生命周期组件（PushService/CommandHandler/ContactEventHandler）"""

    def __init__(self, rec, name, *args, **kwargs):
        self.rec = rec
        self.name = name


class _FingerprintStub:
    def __init__(self, rec, settings, sessdata):
        self.rec = rec

    def get_cookie(self):
        return None


class _BilibiliApiStub:
    def __init__(self, rec, sessdata):
        self.rec = rec
        rec(f"bilibili_api.new:{sessdata}")

    def set_fp_cookie(self, cookie):
        self.rec("bilibili_api.set_fp_cookie")


class _MonitorManagerStub:
    def __init__(self, rec, settings, **kwargs):
        self.rec = rec
        self.push_queue = object()

    async def restore(self, room_ids):
        self.rec(f"monitor_manager.restore:{list(room_ids)}")

    async def ensure(self, room_id, manual=False):
        self.rec(f"monitor_manager.ensure:{room_id}")
        return True, None

    async def stop_all(self):
        self.rec("monitor_manager.stop_all")


class _DynamicManagerStub:
    def __init__(self, rec, settings, push_queue, **kwargs):
        self.rec = rec

    async def ensure(self, up_uid):
        self.rec(f"dynamic_manager.ensure:{up_uid}")
        return True

    async def stop_all(self):
        self.rec("dynamic_manager.stop_all")


class _QQBotStub:
    def __init__(self, rec, app_id, app_secret, debug=False):
        self.rec = rec

    def start(self, *args, **kwargs):
        self.rec("qqbot.start")

    async def stop(self):
        self.rec("qqbot.stop")


class _AdminHandlerStub:
    def __init__(self, rec, *args, **kwargs):
        self.rec = rec

    async def stop(self):
        self.rec("admin_handler.stop")


class _StartStopStub:
    """后台任务组件基类：start()同步、stop()异步"""

    def __init__(self, rec, name, *args, **kwargs):
        self.rec = rec
        self.name = name

    def start(self):
        self.rec(f"{self.name}.start")

    async def stop(self):
        self.rec(f"{self.name}.stop")


class _StreamerRefresherStub(_StartStopStub):
    async def backfill_if_empty(self):
        self.rec(f"{self.name}.backfill_if_empty")


class _GroupRefresherStub(_StartStopStub):
    async def backfill_missing(self):
        self.rec(f"{self.name}.backfill_missing")


class _WebPanelStub:
    def __init__(self, rec, *args, **kwargs):
        self.rec = rec

    async def start(self):
        self.rec("web_panel.start")

    async def stop(self):
        self.rec("web_panel.stop")


# ---------- 桩安装 ----------

def _stub_settings():
    """构造开启全部可选分支的配置（动态监控/周报/Web面板/配置房间）"""
    settings = Settings()
    settings.qqbot.app_id = "1024"
    settings.qqbot.app_secret = "sec"
    settings.auth.sessdata = "cfg-sessdata"
    settings.admin.openids = ["ADMIN1"]
    settings.admin.main_openids = ["MAIN1"]
    settings.message.dynamic_monitor = True   # 走LiveStateWatcher分支
    settings.message.weekly_report = True     # 走DailyReportJob分支
    settings.message.web_panel = True         # 走WebPanel分支
    settings.live.room_id = "23058"           # 走配置房间ensure分支
    return settings


def _install_stubs(monkeypatch, settings, rec, *, fail_ctor=None,
                   redis_sessdata=None, broken_load_exc=None):
    """把__main__命名空间的全部重量组件替换为记录桩

    fail_ctor：指定组件构造时抛RuntimeError（mid-try失败注入）
    broken_load_exc：Settings.load抛出的异常（启动早期失败注入）
    redis_sessdata：get_sessdata_from_redis桩的返回值
    """

    def _ctor(name, factory):
        if fail_ctor != name:
            return factory

        def broken(*args, **kwargs):
            raise RuntimeError(f"{name}构造失败（测试注入）")

        return broken

    monkeypatch.setattr(bm, "Logger", _LoggerStub)

    if broken_load_exc is not None:
        class _BrokenSettings:
            @classmethod
            def load(cls, path):
                raise broken_load_exc

        monkeypatch.setattr(bm, "Settings", _BrokenSettings)
    else:
        class _SettingsStubCls:
            @classmethod
            def load(cls, path):
                rec(f"settings.load:{path}")
                return settings

        monkeypatch.setattr(bm, "Settings", _SettingsStubCls)

    monkeypatch.setattr(bm, "DatabaseManager",
                        _ctor("db_manager", lambda cfg: _DbManagerStub(rec, cfg)))
    monkeypatch.setattr(bm, "DatabaseService",
                        _ctor("db_service", lambda db: _DbServiceStub(rec, db)))
    monkeypatch.setattr(bm, "BindingStore",
                        _ctor("binding_store", lambda db: _BindingStoreStub(rec, db)))
    monkeypatch.setattr(bm, "CookiePool",
                        _ctor("cookie_pool",
                              lambda db: _LoadOnlyStoreStub(rec, "cookie_pool", db)))
    monkeypatch.setattr(bm, "ContactStore",
                        _ctor("contact_store",
                              lambda db: _LoadOnlyStoreStub(rec, "contact_store", db)))
    monkeypatch.setattr(bm, "PushService",
                        _ctor("push_service", lambda s: _PlainStub(rec, "push_service")))
    monkeypatch.setattr(bm, "FingerprintService",
                        _ctor("fingerprint",
                              lambda s, sd: _FingerprintStub(rec, s, sd)))
    monkeypatch.setattr(bm, "BilibiliAPI",
                        _ctor("bilibili_api", lambda sd: _BilibiliApiStub(rec, sd)))
    monkeypatch.setattr(bm, "MonitorManager",
                        _ctor("monitor_manager",
                              lambda s, **kw: _MonitorManagerStub(rec, s, **kw)))
    monkeypatch.setattr(bm, "DynamicManager",
                        _ctor("dynamic_manager",
                              lambda s, q, **kw: _DynamicManagerStub(rec, s, q, **kw)))
    monkeypatch.setattr(bm, "QQBotClient",
                        _ctor("qqbot",
                              lambda aid, sec, debug=False:
                              _QQBotStub(rec, aid, sec, debug=debug)))
    monkeypatch.setattr(bm, "AdminCommandHandler",
                        _ctor("admin_handler",
                              lambda *a, **kw: _AdminHandlerStub(rec, *a, **kw)))
    monkeypatch.setattr(bm, "CommandHandler",
                        _ctor("command_handler",
                              lambda *a, **kw: _PlainStub(rec, "command_handler", *a, **kw)))
    monkeypatch.setattr(bm, "ContactEventHandler",
                        _ctor("contact_handler",
                              lambda *a, **kw: _PlainStub(rec, "contact_handler", *a, **kw)))
    monkeypatch.setattr(bm, "PushQueueConsumer",
                        _ctor("consumer",
                              lambda q, ps, **kw: _StartStopStub(rec, "consumer", q, ps, **kw)))
    monkeypatch.setattr(bm, "DailyReportJob",
                        _ctor("daily_report",
                              lambda *a, **kw: _StartStopStub(rec, "daily_report", *a, **kw)))
    monkeypatch.setattr(bm, "LiveStateWatcher",
                        _ctor("live_watcher",
                              lambda *a, **kw: _StartStopStub(rec, "live_watcher", *a, **kw)))
    monkeypatch.setattr(bm, "WebPanel",
                        _ctor("web_panel",
                              lambda *a, **kw: _WebPanelStub(rec, *a, **kw)))
    monkeypatch.setattr(bm, "CookieMaintainer",
                        _ctor("maintainer",
                              lambda *a, **kw: _StartStopStub(rec, "maintainer", *a, **kw)))
    monkeypatch.setattr(bm, "StreamerRefresher",
                        _ctor("streamer_refresher",
                              lambda *a, **kw:
                              _StreamerRefresherStub(rec, "streamer_refresher", *a, **kw)))
    monkeypatch.setattr(bm, "GroupInfoRefresher",
                        _ctor("group_refresher",
                              lambda *a, **kw:
                              _GroupRefresherStub(rec, "group_refresher", *a, **kw)))

    async def _fake_chromium():
        rec("ensure_playwright_chromium")
        return True

    monkeypatch.setattr(bm, "ensure_playwright_chromium", _fake_chromium)

    def _fake_redis_sessdata(s):
        rec("sessdata.redis")
        return redis_sessdata

    monkeypatch.setattr(bm, "get_sessdata_from_redis", _fake_redis_sessdata)

    async def _fake_listen(stop_event):
        rec("terminal.listen")
        stop_event.set()  # 立即置位：主循环的stop_event.wait()马上返回

    monkeypatch.setattr(bm, "_listen_terminal", _fake_listen)


# ---------- parse_args ----------

def test_parse_args_custom_options(monkeypatch):
    """-c/-s/--log-dir分别解析到config/sessdata/log_dir属性"""
    monkeypatch.setattr(sys, "argv", [
        "blivepost", "-c", "my.yaml", "-s", "SD123", "--log-dir", "dlogs"])
    args = bm.parse_args()
    assert args.config == "my.yaml"
    assert args.sessdata == "SD123"
    assert args.log_dir == "dlogs"


def test_parse_args_defaults(monkeypatch):
    """无参数时：config默认config.yaml、sessdata为None、log_dir默认logs"""
    monkeypatch.setattr(sys, "argv", ["blivepost"])
    args = bm.parse_args()
    assert args.config == "config.yaml"
    assert args.sessdata is None
    assert args.log_dir == "logs"


def test_parse_args_long_aliases(monkeypatch):
    """长参数名--config/--sessdata与短参数等价"""
    monkeypatch.setattr(sys, "argv", [
        "blivepost", "--config", "a.yaml", "--sessdata", "X"])
    args = bm.parse_args()
    assert args.config == "a.yaml"
    assert args.sessdata == "X"


def test_parse_args_version_exits_zero(monkeypatch, capsys):
    """-v打印版本号并以退出码0结束"""
    monkeypatch.setattr(sys, "argv", ["blivepost", "--version"])
    with pytest.raises(SystemExit) as ei:
        bm.parse_args()
    assert ei.value.code == 0
    assert "1.1.0" in capsys.readouterr().out


# ---------- main_async：装配与finally清理 ----------

async def test_main_async_full_startup_ordered_shutdown(monkeypatch):
    """全组件装配成功：各start调用齐全，finally按既定顺序逐个stop后正常返回"""
    rec = _Rec()
    _install_stubs(monkeypatch, _stub_settings(), rec, redis_sessdata=None)
    result = await bm.main_async("config.yaml", None, log_dir="logs")
    assert result is None  # 正常退出不触发sys.exit
    for sig in ("qqbot.start", "consumer.start", "web_panel.start",
                "daily_report.start", "live_watcher.start", "maintainer.start",
                "streamer_refresher.start", "group_refresher.start",
                "terminal.listen",
                "monitor_manager.restore:[23058]", "monitor_manager.ensure:23058",
                "bilibili_api.new:cfg-sessdata"):  # Redis无凭证回落配置文件
        assert sig in rec.calls
    expected_stops = [
        "monitor_manager.stop_all", "admin_handler.stop", "consumer.stop",
        "web_panel.stop", "dynamic_manager.stop_all", "daily_report.stop",
        "live_watcher.stop", "maintainer.stop", "streamer_refresher.stop",
        "group_refresher.stop", "qqbot.stop", "db_service.aclose",
        "db_manager.close",
    ]
    assert rec.calls[-len(expected_stops):] == expected_stops


async def test_main_async_midtry_failure_still_stops_started(monkeypatch):
    """StreamerRefresher构造抛错：SystemExit(1)，此前已启动组件的stop仍按序全调用"""
    rec = _Rec()
    _install_stubs(monkeypatch, _stub_settings(), rec, fail_ctor="streamer_refresher")
    with pytest.raises(SystemExit) as ei:
        await bm.main_async("config.yaml", None, log_dir="logs")
    assert ei.value.code == 1
    assert "maintainer.start" in rec.calls  # 失败点之前组件已启动
    assert "streamer_refresher.start" not in rec.calls
    # 失败点之后两个刷新器保持None被跳过，其余stop一个不少
    expected_stops = [
        "monitor_manager.stop_all", "admin_handler.stop", "consumer.stop",
        "web_panel.stop", "dynamic_manager.stop_all", "daily_report.stop",
        "live_watcher.stop", "maintainer.stop",
        "qqbot.stop", "db_service.aclose", "db_manager.close",
    ]
    assert rec.calls[-len(expected_stops):] == expected_stops


@pytest.mark.parametrize("exc", [
    FileNotFoundError("配置文件 config.yaml 不存在"),
    ValueError("配置字段非法"),
    RuntimeError("任意启动期错误"),
])
async def test_main_async_early_failure_clean_exit(monkeypatch, exc):
    """Settings.load即失败：finally引用全部预初始化None变量不产生NameError，
    三个except分支统一sys.exit(1)，且无任何组件stop被调用"""
    rec = _Rec()
    _install_stubs(monkeypatch, _stub_settings(), rec, broken_load_exc=exc)
    with pytest.raises(SystemExit) as ei:
        await bm.main_async("config.yaml", None, log_dir="logs")
    assert ei.value.code == 1
    assert rec.stop_calls() == []  # 无组件建立；若finally误用未定义变量会先NameError
    assert "db_manager.initialize" not in rec.calls


async def test_main_async_keyboard_interrupt_no_sysexit(monkeypatch):
    """KeyboardInterrupt走安全关闭分支：不sys.exit，main_async正常返回None"""
    rec = _Rec()
    _install_stubs(monkeypatch, _stub_settings(), rec,
                   broken_load_exc=KeyboardInterrupt())
    assert await bm.main_async("config.yaml", None, log_dir="logs") is None
    assert rec.calls == []


async def test_main_async_sessdata_priority(monkeypatch):
    """凭证优先级：命令行-s提供时不查Redis；未提供时取Redis返回值建API"""
    rec = _Rec()
    settings = _stub_settings()
    _install_stubs(monkeypatch, settings, rec, redis_sessdata="redis-value")
    await bm.main_async("config.yaml", "cli-sessdata", log_dir="logs")
    assert "sessdata.redis" not in rec.calls  # 命令行优先，跳过Redis
    assert "bilibili_api.new:cli-sessdata" in rec.calls

    rec2 = _Rec()
    _install_stubs(monkeypatch, settings, rec2, redis_sessdata="redis-value")
    await bm.main_async("config.yaml", None, log_dir="logs")
    assert "sessdata.redis" in rec2.calls  # 无命令行凭证时查Redis
    assert "bilibili_api.new:redis-value" in rec2.calls
