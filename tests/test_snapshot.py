"""快照语义测试：版本固定、发布后新查算用新版、在途查算用旧版、旧库可升级。"""

from __future__ import annotations

import copy
import threading

from hydrology.db import connect, init_database
from hydrology.snapshot import SnapshotStore


def _store_and_conn(tmp_path):
    db_path = str(tmp_path / "snap.db")
    init_database(db_path)
    conn = connect(db_path)
    return db_path, conn, SnapshotStore()


def test_snapshot_pins_versions_at_query_start(tmp_path):
    db_path, conn, store = _store_and_conn(tmp_path)
    snap1 = store.current(conn)
    assert snap1.key == (1, 1)
    # 第二次取到同一个不可变快照对象（进程内复用，索引只建一次）。
    assert store.current(conn) is snap1


def test_new_query_uses_new_version_after_publish_inflight_keeps_old(client, tmp_path):
    # 通过 API 发布一个仅分区表修订：把洪水样本区收窄到 [90,100]×[900,1000]，
    # 推动 rules 指针前进；量程保持不变，(85,850) 的归属随规则版本改变。
    cur = client.get("/api/current").get_json()
    narrowed = copy.deepcopy(cur["rules"])
    for r in narrowed:
        if r["zone"] == "洪水样本区":
            r["wl_min"] = 90
            r["fl_min"] = 900
    resp = client.post("/api/publish/rules", json={"rules": narrowed})
    assert resp.status_code == 200

    # 独立连接 + 独立 store，模拟另一个服务进程。
    db_path = client.application.config["DATABASE"]
    conn = connect(db_path)
    try:
        store = SnapshotStore()
        snap = store.current(conn)
        assert snap.key == (1, 2)
        # 新规则版本下 (85,850) 已不在洪水样本区，落回常规网格。
        verdict = snap.classify(85, 850)
        assert verdict["zone"] == "高水高流量区"
        # 收窄后的洪水样本区仍覆盖 (90,900)。
        assert snap.classify(90, 900)["zone"] == "洪水样本区"

        # 再发一版恢复原始洪水样本区 [85,100]×[850,1000]。
        restored = copy.deepcopy(cur["rules"])
        client.post("/api/publish/rules", json={"rules": restored})

        # 在途旧快照不可变：手里的 rules#2 仍按收窄口径算完。
        assert snap.classify(85, 850)["zone"] == "高水高流量区"

        # 新查算取到新版本（rules#3），(85,850) 重新归入洪水样本区。
        new_snap = store.current(conn)
        assert new_snap.key == (1, 3)
        assert new_snap is not snap
        assert new_snap.classify(85, 850)["zone"] == "洪水样本区"
    finally:
        conn.close()


def test_rejected_publish_keeps_serving_original_version(client):
    before = client.get("/api/current").get_json()
    broken = [r for i, r in enumerate(before["rules"]) if i != 0]
    resp = client.post("/api/publish/rules", json={"rules": broken})
    assert resp.status_code == 422

    after = client.get("/api/current").get_json()
    assert (
        after["range_version_id"],
        after["rules_version_id"],
    ) == (
        before["range_version_id"],
        before["rules_version_id"],
    )
    verdict = client.get("/api/classify?wl=0&flow=0").get_json()
    assert verdict["zone"] == "低水低流量区"


def test_two_processes_each_pin_versions_independently(client, tmp_path):
    db_path = client.application.config["DATABASE"]
    conn_a = connect(db_path)
    conn_b = connect(db_path)
    try:
        store_a, store_b = SnapshotStore(), SnapshotStore()
        snap_a = store_a.current(conn_a)
        snap_b = store_b.current(conn_b)
        assert snap_a.key == snap_b.key == (1, 1)

        cur = client.get("/api/current").get_json()
        extended = copy.deepcopy(cur["rules"])
        for r in extended:
            if r["wl_max"] == 100:
                r["wl_max"] = 130
        client.post("/api/publish/rules", json={"rules": extended})

        # 已持有的快照不变；各自重新查算时独立观察到新版本。
        assert snap_a.key == (1, 1)
        assert store_a.current(conn_a).key == (1, 2)
        assert store_b.current(conn_b).key == (1, 2)
    finally:
        conn_a.close()
        conn_b.close()


def test_snapshot_store_is_thread_safe(tmp_path):
    db_path, _seed_conn, store = _store_and_conn(tmp_path)
    _seed_conn.close()
    try:
        seen = []
        errors = []
        lock = threading.Lock()

        def worker():
            # sqlite3 连接不能跨线程：每个工作线程开自己的连接，共享同一 store。
            conn = connect(db_path)
            try:
                for _ in range(50):
                    key = store.current(conn).key
                    with lock:
                        seen.append(key)
            except Exception as exc:  # pragma: no cover - 并发正确性断言
                with lock:
                    errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert seen and all(key == (1, 1) for key in seen)
    finally:
        pass


def test_legacy_database_without_new_code_is_directly_queryable(tmp_path):
    """模拟旧库升级：用既有 schema 建库播种后，新代码不加迁移即可查算。"""
    from hydrology.db import SCHEMA
    from hydrology.seed import SEED_RANGE, build_seed_rules

    legacy_path = tmp_path / "legacy.db"
    conn = connect(str(legacy_path))
    try:
        conn.executescript(SCHEMA)
        # 直接按旧版本写入方式播种，不经过任何新模块。
        cur = conn.execute(
            """INSERT INTO range_versions
               (wl_min, wl_max, fl_min, fl_max, note, created_at)
               VALUES (?, ?, ?, ?, 'legacy', '2026-01-01T00:00:00+00:00')""",
            (SEED_RANGE.wl_min, SEED_RANGE.wl_max,
             SEED_RANGE.fl_min, SEED_RANGE.fl_max),
        )
        rid = cur.lastrowid
        vid = conn.execute(
            "INSERT INTO rules_versions (note, created_at) VALUES ('legacy', ?)",
            ("2026-01-01T00:00:00+00:00",),
        ).lastrowid
        for r in build_seed_rules():
            conn.execute(
                """INSERT INTO rules
                   (version_id, ord, zone, priority, wl_min, wl_max, fl_min, fl_max)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (vid, r.ord, r.zone, r.priority, r.wl_min, r.wl_max,
                 r.fl_min, r.fl_max),
            )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('current_range_id', ?)", (str(rid),)
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('current_rules_id', ?)", (str(vid),)
        )
    finally:
        conn.close()

    # 新代码直接打开旧库：无 schema 迁移，快照 + 索引可正常查算。
    conn = connect(str(legacy_path))
    try:
        snap = SnapshotStore().current(conn)
        assert snap.key == (rid, vid)
        assert snap.classify(90, 900)["zone"] == "洪水样本区"
        assert snap.classify(10, 100)["zone"] == "低水低流量区"
    finally:
        conn.close()
