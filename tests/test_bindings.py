# -*- coding: utf-8 -*-
"""绑定存储测试"""

import pytest

from blivepost.database import DatabaseManager, DatabaseService
from blivepost.services.bindings import BindingStore
from blivepost.config.settings import Settings


@pytest.fixture
async def store(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    s = BindingStore(DatabaseService(db))
    await s.load()
    yield s, db
    await db.close()


async def test_bind_and_query(store):
    s, _ = store
    await s.bind("GRP_A", 23058, uid=111)
    b = s.get_binding("GRP_A", 23058)
    assert b is not None and b.uid == 111 and not b.monitored
    assert s.get_binding("GRP_A", 1) is None


async def test_bind_upsert_uid(store):
    s, _ = store
    await s.bind("GRP_A", 23058, uid=1)
    await s.bind("GRP_A", 23058, uid=2)
    assert s.get_binding("GRP_A", 23058).uid == 2
    assert len(s.get_group_bindings("GRP_A")) == 1


async def test_monitored_groups_isolated_by_room(store):
    s, _ = store
    await s.bind("GRP_A", 23058, 1)
    await s.bind("GRP_B", 23058, 1)
    await s.bind("GRP_A", 99, 2)
    assert s.get_monitored_groups(23058) == []
    await s.set_monitored("GRP_A", 23058, True)
    assert s.get_monitored_groups(23058) == ["GRP_A"]
    await s.set_monitored("GRP_B", 23058, True)
    assert sorted(s.get_monitored_groups(23058)) == ["GRP_A", "GRP_B"]
    assert s.get_monitored_groups(99) == []


async def test_unbind(store):
    s, _ = store
    await s.bind("GRP_A", 23058, 1)
    assert await s.unbind("GRP_A", 23058) is True
    assert await s.unbind("GRP_A", 23058) is False
    assert s.get_binding("GRP_A", 23058) is None


async def test_monitored_room_ids_dedup(store):
    s, _ = store
    await s.bind("GRP_A", 100, 1)
    await s.bind("GRP_B", 100, 1)
    await s.bind("GRP_A", 200, 2)
    await s.bind("GRP_C", 300, 3)
    await s.set_monitored("GRP_A", 100, True)
    await s.set_monitored("GRP_B", 100, True)
    await s.set_monitored("GRP_A", 200, True)
    assert s.get_monitored_room_ids() == [100, 200]


async def test_persistence_across_reload(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    service = DatabaseService(db)

    s = BindingStore(service)
    await s.load()
    await s.bind("GRP_A", 23058, uid=111)
    await s.set_monitored("GRP_A", 23058, True)

    # 重新构造存储并加载，验证持久化
    s2 = BindingStore(service)
    await s2.load()
    b = s2.get_binding("GRP_A", 23058)
    assert b is not None and b.uid == 111 and b.monitored
    assert s2.get_monitored_groups(23058) == ["GRP_A"]
    await db.close()
