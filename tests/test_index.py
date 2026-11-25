"""双轴空间索引测试：与暴力扫描等价、稀疏命中只查局部候选、整数边界、线性空间。"""

from __future__ import annotations

import random

from hydrology.domain import Range, Rule, classify_pair, classify_rules
from hydrology.index import SpatialIndex
from hydrology.seed import SEED_RANGE, build_seed_rules


def _brute_force(rules, wl, fl):
    return [r for r in rules if r.covers(wl, fl)]


def _make_grid(n_wl=12, n_fl=12, span=1000, seed=7):
    """规则在两轴均匀分散的网格：每个单元格一条互不重叠的规则。"""
    rng = random.Random(seed)
    rules = []
    wl_step = span // n_wl
    fl_step = span // n_fl
    seq = 0
    for i in range(n_wl):
        for j in range(n_fl):
            lo_w = i * wl_step
            hi_w = (i + 1) * wl_step - 1 if i < n_wl - 1 else span
            lo_f = j * fl_step
            hi_f = (j + 1) * fl_step - 1 if j < n_fl - 1 else span
            rules.append(
                Rule(
                    zone=f"格{i}-{j}",
                    priority=10,
                    wl_min=lo_w,
                    wl_max=hi_w,
                    fl_min=lo_f,
                    fl_max=hi_f,
                    ord=seq,
                )
            )
            seq += 1
    return Range(0, span, 0, span), rules


def test_index_matches_brute_force_on_seed_rules():
    rules = build_seed_rules()
    idx = SpatialIndex(rules)
    points = [
        (0, 0), (49, 399), (50, 400), (84, 849), (85, 850),
        (90, 900), (100, 1000), (86, 849), (1, 999), (79, 799),
    ]
    for wl, fl in points:
        via_index = idx.query(wl, fl)
        via_scan = _brute_force(rules, wl, fl)
        assert [r.zone for r in via_index] == [r.zone for r in via_scan], (wl, fl)


def test_index_matches_brute_force_on_random_sparse_grid():
    rng_bounds, rules = _make_grid(n_wl=15, n_fl=15, span=3000)
    idx = SpatialIndex(rules)
    assert idx.rule_count == len(rules)

    rng = random.Random(2026)
    for _ in range(500):
        wl = rng.randint(0, 3000)
        fl = rng.randint(0, 3000)
        got = [(r.wl_min, r.fl_min) for r in idx.query(wl, fl)]
        want = [(r.wl_min, r.fl_min) for r in _brute_force(rules, wl, fl)]
        assert got == want, (wl, fl)


def test_integer_closed_interval_endpoints_are_included():
    rng = Range(0, 100, 0, 100)
    rules = [Rule("R", 1, 10, 20, 30, 40, ord=0)]
    idx = SpatialIndex(rules)

    # 四个闭区间端点都必须命中（含左下、右上两个角点）。
    for wl, fl in [(10, 30), (10, 40), (20, 30), (20, 40), (15, 35)]:
        assert [r.zone for r in idx.query(wl, fl)] == ["R"]

    # 端点外侧相邻整数不命中。
    for wl, fl in [(9, 35), (21, 35), (15, 29), (15, 41)]:
        assert idx.query(wl, fl) == []


def test_sparse_hit_only_inspects_local_candidates():
    _, rules = _make_grid(n_wl=20, n_fl=20, span=10_000)
    idx = SpatialIndex(rules)
    assert idx.rule_count == 400

    # 网格互不重叠：每个点恰好 1 条规则命中。
    stats = idx.query_stats(5_025, 7_025)
    assert stats["matched"] == 1
    assert stats["covers_checked"] == 1
    # 交集候选应远小于全表 400 条：双轴刺查只触及局部的少数规则。
    assert stats["intersection_candidates"] <= 4
    assert stats["union_candidates"] < idx.rule_count

    # 无命中的点：交集为空，一条 covers 都不必复核。
    empty = idx.query_stats(20_000, 20_000)
    assert empty["matched"] == 0
    assert empty["intersection_candidates"] == 0
    assert empty["covers_checked"] == 0


def test_index_storage_is_linear_in_rule_count():
    # 用内部节点/端点数组的总长度衡量索引空间：应随规则条数线性增长。
    def index_size(n):
        _, rules = _make_grid(n_wl=n, n_fl=n, span=5_000, seed=11)
        idx = SpatialIndex(rules)

        def tree_size(node):
            if node is None:
                return 0
            return (
                len(node.by_lo)
                + len(node.by_hi)
                + tree_size(node.left)
                + tree_size(node.right)
            )

        return tree_size(idx._wl_tree.root) + tree_size(idx._fl_tree.root), len(rules)

    size_small, n_small = index_size(5)
    size_large, n_large = index_size(20)
    # 每条规则在每棵树恰好存两份序号（by_lo + by_hi），总量 = 4n，严格线性。
    assert size_small == 4 * n_small
    assert size_large == 4 * n_large
    ratio = (size_large / n_large) / (size_small / n_small)
    assert abs(ratio - 1.0) < 1e-9


def test_indexed_and_linear_classify_return_identical_payload():
    rules = build_seed_rules()
    idx = SpatialIndex(rules)
    for wl, fl in [(90, 900), (85, 850), (84, 849), (10, 100), (100, 1000)]:
        via_scan = classify_pair(SEED_RANGE, rules, wl, fl)
        via_index = classify_rules(SEED_RANGE, rules, wl, fl, index=idx)
        assert via_index == via_scan, (wl, fl)


def test_empty_rules_index_queries_safely():
    idx = SpatialIndex([])
    assert idx.rule_count == 0
    assert idx.query(0, 0) == []
    stats = idx.query_stats(0, 0)
    assert stats["rule_count"] == 0 and stats["matched"] == 0


def test_query_preserves_original_rule_order():
    # 重叠规则：索引返回顺序应与传入（ord）顺序一致，裁决仍交给上层。
    rules = [
        Rule("低优先级大框", 10, 0, 100, 0, 100, ord=0),
        Rule("高优先级小框", 1, 40, 60, 40, 60, ord=1),
        Rule("中优先级条带", 5, 50, 80, 0, 100, ord=2),
    ]
    idx = SpatialIndex(rules)
    zones = [r.zone for r in idx.query(55, 55)]
    assert zones == ["低优先级大框", "高优先级小框", "中优先级条带"]
