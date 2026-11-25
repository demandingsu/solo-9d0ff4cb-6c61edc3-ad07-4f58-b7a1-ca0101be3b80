#!/usr/bin/env python3
"""交付前自测 / 演示脚本。

用临时 SQLite 库 + Flask test client 实际跑：
  1. 现行口径摘要与若干对读数的分区判定（含洪水优先）；
  2. 漏分见证点、最高级并列见证点（各给可复现读数对与最高级分区）；
  3. 两类修订交错提交：失败驳回 -> 沿用现行 -> 换序成功；
  4. 核对进行期间连续查单条读数，确认网页/API 不受影响；
  5. 主要页面冒烟。

退出码 0 表示全部断言通过。
"""

from __future__ import annotations

import copy
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydrology import create_app  # noqa: E402
from hydrology.domain import (  # noqa: E402
    Range,
    Rule,
    classify_pair,
    classify_rules,
    verify,
)
from hydrology.index import SpatialIndex  # noqa: E402
from hydrology.seed import SEED_RANGE as SEED_RANGE_FOR_SELFTEST  # noqa: E402
from hydrology.seed import build_seed_rules  # noqa: E402


def build_seed_rules_for_selftest(current_payload):
    """把 /api/current 的规则字典还原为 Rule 对象（保持 ord 顺序）。"""
    return [
        Rule(
            zone=r["zone"],
            priority=r["priority"],
            wl_min=r["wl_min"],
            wl_max=r["wl_max"],
            fl_min=r["fl_min"],
            fl_max=r["fl_max"],
            ord=r.get("ord"),
            id=r.get("rule_id"),
        )
        for r in current_payload["rules"]
    ]


def snapshot_classify_equiv(rules, wl, fl):
    """与 Snapshot.classify 相同的索引路径（此处直接构造索引做等价对照）。"""
    return classify_rules(
        SEED_RANGE_FOR_SELFTEST, rules, wl, fl, index=SpatialIndex(rules)
    )

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="hydrology-selftest-")
    db_path = str(Path(tmpdir) / "selftest.db")
    app = create_app({"TESTING": True, "DATABASE": db_path})
    client = app.test_client()
    print(f"临时 SQLite：{db_path}")

    # ------------------------------------------------------------------ #
    banner("1. 现行口径与分区判定")
    # ------------------------------------------------------------------ #
    current = client.get("/api/current").get_json()
    rng = current["range"]
    rules = current["rules"]
    print(
        f"现行量程：水位 {rng['water_level']}，流量 {rng['flow']}；"
        f"规则 {len(rules)} 条；版本 range#{current['range_version_id']} "
        f"rules#{current['rules_version_id']}"
    )

    cases = [
        (90, 900, "洪水样本区"),
        (85, 850, "洪水样本区（闭区间下界）"),
        (84, 849, "高水高流量区"),
        (82, 820, "高水高流量区"),
        (60, 500, "中水中流量区"),
        (10, 100, "低水低流量区"),
    ]
    for wl, fl, expected in cases:
        d = client.get(f"/api/classify?wl={wl}&flow={fl}").get_json()
        zone = d.get("zone")
        ok = zone == expected or (expected.endswith("）") and zone == expected.split("（")[0])
        check(
            f"判定 ({wl:>3}, {fl:>4}) -> {zone}",
            d.get("status") == "classified" and (zone == expected or expected.startswith(zone)),
            f"期望 {expected}",
        )

    out = client.get("/api/classify?wl=150&flow=100").get_json()
    check("超量程读数被识别", out["status"] == "out_of_range", out.get("message", ""))

    # ------------------------------------------------------------------ #
    banner("2. 核对：漏分 / 最高级并列的可复现见证")
    # ------------------------------------------------------------------ #
    gap_rules = [r for i, r in enumerate(copy.deepcopy(rules)) if i != 0]
    gap_resp = client.post("/api/publish/rules", json={"rules": gap_rules})
    gap_report = gap_resp.get_json()["report"]
    gw = gap_report["gap"]
    print(f"  漏分见证：水位={gw['water_level']}, 流量={gw['flow']}  ({gw['reproduce']})")
    check("漏分稿被驳回(422)", gap_resp.status_code == 422)
    check("漏分见证读数对为 (0,0)", (gw["water_level"], gw["flow"]) == (0, 0))
    again = verify(
        Range(0, 100, 0, 1000),
        [
            Rule(
                zone=r["zone"], priority=r["priority"],
                wl_min=r["wl_min"], wl_max=r["wl_max"],
                fl_min=r["fl_min"], fl_max=r["fl_max"], ord=r.get("ord"),
            )
            for r in gap_rules
        ],
    )
    check(
        "漏分见证可复现",
        (again["gap"]["water_level"], again["gap"]["flow"])
        == (gw["water_level"], gw["flow"]),
    )

    tie_rules = copy.deepcopy(rules)
    tie_rules.append(
        {
            "zone": "并列洪峰复核区", "priority": 1,
            "wl_min": 90, "wl_max": 100, "fl_min": 900, "fl_max": 1000,
        }
    )
    tie_resp = client.post("/api/publish/rules", json={"rules": tie_rules})
    tie_report = tie_resp.get_json()["report"]
    tw = tie_report["tie"]
    print(
        f"  并列见证：水位={tw['water_level']}, 流量={tw['flow']}，"
        f"最高级(p={tw['priority']})分区：{'、'.join(tw['top_zones'])}"
    )
    check("并列稿被驳回(422)", tie_resp.status_code == 422)
    check("并列见证读数对为 (90,900)", (tw["water_level"], tw["flow"]) == (90, 900))
    check(
        "并列列出两个最高级分区",
        set(tw["top_zones"]) == {"洪水样本区", "并列洪峰复核区"},
    )
    still = client.get("/api/classify?wl=90&flow=900").get_json()
    check("驳回后现行 (90,900) 仍唯一归洪水样本区", still.get("zone") == "洪水样本区")

    # ------------------------------------------------------------------ #
    banner("3. 交错提交：每笔发布前用另一项届时生效版本复核")
    # ------------------------------------------------------------------ #
    ids0 = client.get("/api/current").get_json()
    r0, v0 = ids0["range_version_id"], ids0["rules_version_id"]
    print(f"  起点现行指针：range#{r0}, rules#{v0}")

    # 3.1 先扩量程 -> 现行规则盖不住 -> 失败沿用现行
    resp = client.post(
        "/api/publish/ranges",
        json={"wl_min": 0, "wl_max": 200, "fl_min": 0, "fl_max": 1000,
              "note": "交错A：先扩量程"},
    )
    b = resp.get_json()
    ids1 = client.get("/api/current").get_json()
    check(
        "先扩量程被驳回（新条带漏分）",
        resp.status_code == 422 and b["report"]["gap"]["water_level"] == 101,
        f"见证 ({b['report']['gap']['water_level']}, {b['report']['gap']['flow']})",
    )
    check(
        "驳回后两个指针均不变",
        (ids1["range_version_id"], ids1["rules_version_id"]) == (r0, v0),
    )

    # 3.2 改为先发延伸规则（按现行量程 0..100 复核通过）
    extended = copy.deepcopy(rules)
    for r in extended:
        if r["wl_max"] == 100:
            r["wl_max"] = 200
    resp = client.post(
        "/api/publish/rules",
        json={"rules": extended, "note": "交错B：先延伸规则"},
    )
    ids2 = client.get("/api/current").get_json()
    check("单独发布延伸规则通过", resp.status_code == 200)
    check(
        "仅发规则：量程指针不动、规则指针前进",
        ids2["range_version_id"] == r0 and ids2["rules_version_id"] > v0,
        f"range#{ids2['range_version_id']} rules#{ids2['rules_version_id']}",
    )

    # 3.3 再发同一量程 -> 用届时生效的新规则复核 -> 通过
    resp = client.post(
        "/api/publish/ranges",
        json={"wl_min": 0, "wl_max": 200, "fl_min": 0, "fl_max": 1000,
              "note": "交错C：规则就位后再扩量程"},
    )
    ids3 = client.get("/api/current").get_json()
    check("规则就位后扩量程通过", resp.status_code == 200)
    check(
        "量程指针前进、规则指针保持",
        ids3["range_version_id"] > r0
        and ids3["rules_version_id"] == ids2["rules_version_id"],
    )
    verdict = client.get("/api/classify?wl=150&flow=900").get_json()
    check("新条带 (150,900) 归洪水样本区", verdict.get("zone") == "洪水样本区")

    # 3.4 一并发布但内部不匹配 -> 两个指针都不动
    ra, va = ids3["range_version_id"], ids3["rules_version_id"]
    resp = client.post(
        "/api/publish/bundle",
        json={
            "wl_min": 0, "wl_max": 400, "fl_min": 0, "fl_max": 1000,
            "rules": ids3["rules"], "note": "bundle 故意只扩量程",
        },
    )
    ids4 = client.get("/api/current").get_json()
    check("不匹配的一并发布被驳回", resp.status_code == 422)
    check(
        "bundle 驳回后两个指针都不动",
        (ids4["range_version_id"], ids4["rules_version_id"]) == (ra, va),
    )

    # ------------------------------------------------------------------ #
    banner("4. 核对期间网页/API 照常查现行归属")
    # ------------------------------------------------------------------ #
    big_range = Range(0, 200_000, 0, 200_000)
    heavy = [
        Rule(
            zone=f"压力规则{i}", priority=10_000 + i,
            wl_min=i * 500, wl_max=200_000 - i * 260,
            fl_min=i * 500, fl_max=200_000 - i * 260, ord=i,
        )
        for i in range(80)
    ]

    def heavy_worker():
        for _ in range(2):
            verify(big_range, heavy)

    t = threading.Thread(target=heavy_worker)
    t.start()
    during_ok = 0
    during_bad = 0
    started = time.perf_counter()
    while t.is_alive():
        d = client.get("/api/classify?wl=90&flow=900")
        if d.status_code == 200 and d.get_json().get("zone") == "洪水样本区":
            during_ok += 1
        else:
            during_bad += 1
    t.join()
    elapsed = time.perf_counter() - started
    # 线程结束后再补若干请求
    after_ok = 0
    for _ in range(20):
        d = client.get("/api/classify?wl=10&flow=100")
        if d.status_code == 200:
            after_ok += 1
    check(
        f"重型核对运行的 {elapsed:.2f}s 窗口内连续查算 {during_ok} 次全部正确"
        f"（失败 {during_bad}），窗口后 {after_ok}/20 正常",
        during_bad == 0 and during_ok >= 1 and after_ok == 20,
    )

    # ------------------------------------------------------------------ #
    banner("5. 双轴空间索引：稀疏命中只查局部候选、整数闭区间边界")
    # ------------------------------------------------------------------ #
    seed_rules = build_seed_rules_for_selftest(current)
    index = SpatialIndex(seed_rules)

    # 5.1 索引结果与暴力全表扫描在一批点上完全一致（含全部区间端点）。
    boundary_points = [
        (0, 0), (49, 399), (50, 400), (79, 799), (80, 800),
        (84, 849), (85, 850), (86, 850), (85, 849), (90, 900),
        (100, 1000), (100, 850), (85, 1000), (49, 400), (50, 399),
    ]
    equivalent = True
    for wl, fl in boundary_points:
        via_index = [r.zone for r in index.query(wl, fl)]
        via_scan = [r.zone for r in seed_rules if r.covers(wl, fl)]
        if via_index != via_scan:
            equivalent = False
            print(f"    不一致点 ({wl},{fl}): index={via_index} scan={via_scan}")
    check("索引与全表扫描在 15 个边界点上结果完全一致", equivalent)

    # 5.2 整数闭区间：四个角点全部命中，端点外侧相邻整数不命中。
    flood = next(r for r in seed_rules if r.zone == "洪水样本区")
    corners = [(85, 850), (85, 1000), (100, 850), (100, 1000)]
    corner_ok = all(
        any(r.zone == "洪水样本区" for r in index.query(wl, fl))
        for wl, fl in corners
    )
    outside_ok = all(index.query(wl, fl) and not any(
        r.zone == "洪水样本区" for r in index.query(wl, fl)
    ) for wl, fl in [(84, 900), (90, 849)])
    check("洪水样本区闭区间四角点命中、外侧相邻整数不命中", corner_ok and outside_ok)

    # 5.3 稀疏分散的大规则集：单点命中稀疏时，复核的候选远少于规则总数。
    sparse_rules = []
    n_per_axis = 20
    step = 20_000 // n_per_axis
    seq = 0
    for i in range(n_per_axis):
        for j in range(n_per_axis):
            sparse_rules.append(
                Rule(
                    zone=f"网格{i}-{j}", priority=10,
                    wl_min=i * step,
                    wl_max=(i + 1) * step - 1 if i < n_per_axis - 1 else 20_000,
                    fl_min=j * step,
                    fl_max=(j + 1) * step - 1 if j < n_per_axis - 1 else 20_000,
                    ord=seq,
                )
            )
            seq += 1
    sparse_index = SpatialIndex(sparse_rules)
    stats = sparse_index.query_stats(5_025, 12_025)
    print(
        f"    规则总数 {stats['rule_count']}；该点水位轴候选 "
        f"{stats['wl_axis_candidates']}、流量轴候选 {stats['fl_axis_candidates']}、"
        f"交集候选 {stats['intersection_candidates']}、闭区间复核 "
        f"{stats['covers_checked']} 条、命中 {stats['matched']} 条"
    )
    check(
        "稀疏命中：交集候选与复核条数远小于规则总数（只查局部候选）",
        stats["rule_count"] == 400
        and stats["matched"] == 1
        and stats["covers_checked"] == 1
        and stats["intersection_candidates"] <= 4,
    )
    empty_stats = sparse_index.query_stats(50_000, 50_000)
    check(
        "无命中点零候选复核",
        empty_stats["intersection_candidates"] == 0
        and empty_stats["covers_checked"] == 0,
    )

    # 5.4 索引存储随规则条数线性：每条规则在每棵树恰占两个序号位。
    def tree_nodes(node):
        if node is None:
            return 0
        return (
            len(node.by_lo) + len(node.by_hi)
            + tree_nodes(node.left) + tree_nodes(node.right)
        )

    index_slots = tree_nodes(sparse_index._wl_tree.root) + tree_nodes(
        sparse_index._fl_tree.root
    )
    check(
        "索引空间随规则条数线性增长（序号槽位 = 4 × 规则数）",
        index_slots == 4 * len(sparse_rules),
        f"{index_slots} slots / {len(sparse_rules)} rules",
    )

    # 5.5 经索引与经线性扫描的 classify 载荷完全相同。
    payload_same = all(
        classify_pair(SEED_RANGE_FOR_SELFTEST, seed_rules, wl, fl)
        == snapshot_classify_equiv(seed_rules, wl, fl)
        for wl, fl in boundary_points
    )
    check("索引路径与线性路径的查算响应字典完全一致", payload_same)

    # ------------------------------------------------------------------ #
    banner("6. 首页分区表懒加载（点开才加载，首屏不读全表）")
    # ------------------------------------------------------------------ #
    home = client.get("/")
    check("首页首屏含懒加载折叠面板", b'id="rules-panel"' in home.data)
    check("首页首屏不渲染规则行（无洪水样本区字样）", "洪水样本区".encode() not in home.data)
    check("首页首屏仍展示规则条数", "共 10 条规则".encode() in home.data)
    frag = client.get("/rules-table")
    check(
        "GET /rules-table 返回分区表片段（10 条规则、无整页骨架）",
        frag.status_code == 200
        and "洪水样本区".encode() in frag.data
        and b"<html" not in frag.data,
    )
    check("首页保留 <noscript> 无脚本回退", b"<noscript>" in home.data)

    # ------------------------------------------------------------------ #
    banner("7. 页面冒烟")
    # ------------------------------------------------------------------ #
    for path, token in [
        ("/", "现行分区表"),
        ("/publish", "发布口径修订"),
        ("/attempts", "发布与核对记录"),
        ("/attempts/1", "初始播种"),
    ]:
        html = client.get(path)
        ok = html.status_code == 200 and token.encode() in html.data
        check(f"GET {path} -> 200 且含“{token}”", ok)

    # ------------------------------------------------------------------ #
    banner("自测汇总")
    # ------------------------------------------------------------------ #
    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"共 {len(CHECKS)} 项，通过 {len(CHECKS) - len(failed)} 项，失败 {len(failed)} 项。")
    if failed:
        for name in failed:
            print("  FAIL:", name)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
