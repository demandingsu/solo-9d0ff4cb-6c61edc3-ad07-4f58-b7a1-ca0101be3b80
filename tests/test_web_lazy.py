"""首页懒加载分区表、首屏不读全表、首页与 API 共用索引路径的测试。"""

from __future__ import annotations


def test_index_first_paint_does_not_render_rule_rows(client):
    resp = client.get("/")
    assert resp.status_code == 200
    # 折叠面板与懒加载挂载点存在，但规则行不在首屏 HTML 中。
    assert b'id="rules-panel"' in resp.data
    assert b'data-src="/rules-table"' in resp.data
    assert "洪水样本区".encode() not in resp.data
    # 规则条数仍以轻量计数形式展示。
    assert "共 10 条规则".encode() in resp.data


def test_rules_table_fragment_loads_on_demand(client):
    resp = client.get("/rules-table")
    assert resp.status_code == 200
    assert "洪水样本区".encode() in resp.data
    assert b"<table" in resp.data
    # 片段是局部模板，不重复渲染整页骨架。
    assert b"<html" not in resp.data
    rows = resp.data.count(b"<tr ") + resp.data.count(b"<tr>")
    # 含表头 1 行 + 播种口径 10 条规则。
    assert rows == 11
    assert resp.data.count(b"flood-row") == 1


def test_noscript_fallback_keeps_table_accessible(client):
    # 折叠面板内嵌 noscript 回退，无 JS 时资料员仍能看到整张表。
    resp = client.get("/")
    assert b"<noscript>" in resp.data


def test_index_and_api_share_snapshot_index_and_agree(client):
    # 同一进程内两个入口命中同一快照对象（同一版本 -> 同一索引）。
    api_resp = client.get("/api/classify?wl=90&flow=900").get_json()
    page = client.get("/?wl=90&flow=900")
    assert page.status_code == 200
    assert api_resp["zone"] == "洪水样本区"
    assert "洪水样本区".encode() in page.data

    # 直接断言两个入口背后的 store 中只有一个现行快照对象。
    store = client.application.extensions["hydrology_snapshot_store"]
    assert (1, 1) in store._cache
    snap = store._cache[(1, 1)]
    assert snap.index.rule_count == 10


def test_index_overview_uses_count_not_full_table_scan(client, monkeypatch):
    # 拦截 db 模块的连接执行，断言首屏只跑 COUNT 而不读 rules 全表。
    import hydrology.db as db_module

    real_connect = db_module.connect
    executed: list[str] = []

    class RecordingConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith("SELECT"):
                executed.append(sql.upper())
            return self._inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def fake_connect(path):
        return RecordingConn(real_connect(path))

    monkeypatch.setattr(db_module, "connect", fake_connect)
    # web 模块通过函数名引用 get_db -> connect，确保拿到的是被拦截的连接。
    resp = client.get("/")
    assert resp.status_code == 200

    def selects_rule_rows(sql: str) -> bool:
        # 精确匹配对 rules 表的取行查询（SELECT *），排除 COUNT 聚合与
        # rules_versions 表；容忍换行与多余空白。
        compact = " ".join(sql.split())
        return compact.startswith("SELECT * FROM RULES ") or compact.startswith(
            "SELECT * FROM RULES WHERE"
        )

    full_scans = [sql for sql in executed if selects_rule_rows(sql)]
    assert full_scans == [], "首页首屏不应读取 rules 全表"
    assert any("COUNT(*)" in sql for sql in executed)
