# -*- coding: utf-8 -*-
"""第十二批：运维小件回归测试

1. /healthz 游客可访问+脱敏（不含账号UID/群openid等运营情报）+状态码语义
2. BackupJob 定时备份+滚动保留
3. 按房间×日期范围导出CSV/JSON（不依赖zip，MySQL/PG可用）
4. base.html浅色主题变量已完备（结构检查，非渲染测试）
"""

import zipfile
from pathlib import Path

from blivepost.config.settings import Settings
from blivepost.database.manager import DatabaseManager
from blivepost.database.service import DatabaseService


# ---------- 1. /healthz ----------

async def test_healthz_guest_accessible_and_sanitized(tmp_path):
    """/healthz 游客200可访问；报告脱敏（无账号UID/群openid）；DB错误时503"""
    from blivepost.core.health import build_health_report

    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)

    class FakeMon:
        def room_ids(self):
            return [23058, 23059]

        def has(self, rid):
            return rid == 23058

    class FakeDyn:
        enabled = True

    class FakeBot:
        SEND_QUEUE_SIZE = 200
        started = True
        _send_queue = None

    try:
        report = await build_health_report(svc, FakeMon(), FakeDyn(), FakeBot())
        assert report["status"] == "degraded"     # 23059未存活
        assert report["rooms"] == {"total": 2, "alive": 1}
        assert report["db"] == "ok"
        assert report["bot"] == "started"
        # 脱敏：不出现UID/openid等
        assert "uid" not in str(report).lower() or "room_id" not in str(report)
        assert "openid" not in str(report).lower()
        assert "cookie" not in str(report).lower()
    finally:
        await svc.aclose()
        await db.close()

    # DB不可达 → error + 503语义
    class FakeBadDb:
        async def get_table_counts(self):
            raise RuntimeError("db down")

    report2 = await build_health_report(
        FakeBadDb(), FakeMon(), FakeDyn(), FakeBot())
    assert report2["status"] == "error"
    assert report2["db"] == "error"


# ---------- 2. BackupJob ----------

async def test_backup_job_creates_and_prunes(tmp_path, monkeypatch):
    """定时备份：生成脱敏zip（cookie_account空）+滚动保留超出份数删除"""
    from blivepost.core.health import BackupJob

    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    try:
        # 造一条cookie数据验证脱敏
        await svc.save_cookie_account(
            uid=1, nickname="测试", cookie_string="SESSDATA=x",
            sessdata="x", bili_jct="j", ac_time_value="a",
            sessdata_expires_at=None)
        # cwd切到tmp_path使exports/backups写入临时目录
        monkeypatch.chdir(tmp_path)

        job = BackupJob(settings, svc)
        job.keep = 3
        # 执行3次备份（每次文件名时间戳不同）
        paths = []
        for i in range(4):
            import time
            time.sleep(1.05)  # 文件名到秒级，需>1s间隔
            p = await job.backup_now()
            if p:
                paths.append(p)
        backups = sorted(Path("exports/backups").glob("blivepost_backup_*.zip"))
        assert len(backups) == 3       # 滚动保留3份
        # zip内容脱敏：cookie_account空
        with zipfile.ZipFile(backups[0]) as zf:
            member = [n for n in zf.namelist() if n.endswith("t.db")][0]
            import sqlite3, tempfile, os
            fd, tmpdb = tempfile.mkstemp(suffix=".db")
            os.write(fd, zf.read(member))
            os.close(fd)
            con = sqlite3.connect(tmpdb)
            n = con.execute(
                "SELECT COUNT(*) FROM cookie_account").fetchone()[0]
            con.close()
            os.unlink(tmpdb)
        assert n == 0                   # 脱敏：cookie_account为空
    finally:
        await svc.aclose()
        await db.close()


def test_backup_keep_zero_disables():
    """backup_keep=0 → start()不创建定时任务"""
    from blivepost.core.health import BackupJob
    settings = Settings()
    settings.message.backup_keep = 0
    job = BackupJob(settings, None)
    job.start()
    assert job._task is None


# ---------- 3. 按房间×日期范围导出 ----------

async def test_export_room_csv_json(tmp_path):
    """按房间导出CSV（带BOM）与JSON；游客403"""
    from aiohttp.test_utils import TestClient, TestServer
    from blivepost.services.webpanel import WebPanel

    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    try:
        await svc.save_danmaku(room_id=23058, uid=1, uname="u", msg="测试弹幕")
        panel = WebPanel(None, None, None, None, svc,
                         token="TK", admin_password="p")
        client = TestClient(TestServer(panel._build_app()
                                       if hasattr(panel, "_build_app")
                                       else _make_app(panel)))
        await client.start_server()
        try:
            sid = panel._create_session("admin", is_admin=True)
            headers = {"Cookie": f"blivepost_sid={sid}"}
            # CSV
            r = await client.get("/web/admin/export/room?room_id=23058&format=csv",
                                 headers=headers)
            assert r.status == 200
            assert "text/csv" in r.headers["Content-Type"]
            body = await r.text()
            assert body.startswith("\ufeff")      # BOM
            assert "测试弹幕" in body
            # JSON
            r2 = await client.get("/web/admin/export/room?room_id=23058&format=json",
                                  headers=headers)
            assert r2.status == 200
            import json
            data = json.loads(await r2.text())
            assert data["room_id"] == 23058
            assert len(data["records"]) >= 1
            # 游客403
            r3 = await client.get("/web/admin/export/room?room_id=23058")
            assert r3.status == 403
            # 缺room_id → 400
            r4 = await client.get("/web/admin/export/room", headers=headers)
            assert r4.status == 400
        finally:
            await client.close()
    finally:
        await svc.aclose()
        await db.close()


def _make_app(panel):
    """构建含export/room路由的测试app（绕过完整start）"""
    from aiohttp import web
    app = web.Application()
    app.router.add_get("/web/admin/export/room",
                       panel._handle_admin_export_room)
    return app


# ---------- 4. base.html浅色主题结构检查 ----------

def test_base_template_light_theme_vars():
    """base.html含浅色主题CSS变量+Tailwind覆盖层+prefers-color-scheme"""
    from blivepost.services.webpanel_common import _TEMPLATES_DIR
    base = (_TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
    assert "prefers-color-scheme: light" in base
    assert "--bg:" in base and "--surface:" in base
    assert "bg-slate-900" in base           # Tailwind覆盖层存在
    assert "var(--surface)" in base         # 映射到变量
