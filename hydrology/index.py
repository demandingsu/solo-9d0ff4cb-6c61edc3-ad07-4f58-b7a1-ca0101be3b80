"""双轴区间空间索引：让单点查算只检查局部候选，而不是扫描整张规则表。

背景
----
旧版 ``classify_pair`` 对每对读数都线性扫描全部规则（O(n)）。当规则在
水位 / 流量两轴上分散、单点命中稀疏时，绝大多数规则与查询点毫无关系，
却仍要逐条比较四个边界。

本模块提供一个纯标准库、无外部依赖的 **双轴居中区间树（centered interval
tree）**：

* 每条规则按其水位闭区间进水位树、按流量闭区间进流量树；
* 单点刺查（stabbing query）沿一条根到叶的路径下行，只访问区间端点
  排序列表中与查询点相交的前缀 / 后缀，命中稀疏时只需检查极少数候选；
* 最终候选取两轴刺查结果的交集，再做一次防御性的闭区间 ``covers`` 复核。

复杂度
------
* 建树：每轴 O(n log n)，**索引存储空间 O(n)**（每条规则在每棵树中恰好
  存一份；连同两轴的节点与端点数组，总量仍随规则条数线性增长）；
* 单点查询：O(log n + k)，``k`` 为该轴上与查询点相交的规则数；两轴取交后
  实际做闭区间复核的候选为两轴交集，命中稀疏时 ``k`` 远小于 ``n``。

索引对象对一组**已固定**的规则只构建一次、不可变，可被多个线程安全共享；
版本切换时由快照层整体替换，旧索引随旧快照继续服务在途查算。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Optional, Sequence

from .domain import Rule


@dataclass(frozen=True)
class _Node:
    """居中区间树的一个节点。

    ``by_lo`` / ``by_hi`` 保存中心落在本节点的规则下标（规则在索引内的
    稳定序号），分别按区间左端点升序、右端点降序排列，配合二分把逐条
    比较收敛为“与查询点相交的那一段”。
    """

    center: int
    by_lo: tuple[int, ...]          # 按 (wl_min / fl_min) 升序
    by_hi: tuple[int, ...]          # 按 (wl_max / fl_max) 降序
    left: Optional["_Node"]
    right: Optional["_Node"]


class _AxisIntervalTree:
    """单轴闭区间刺查树（整数点 p 命中区间 [lo, hi] 当且仅当 lo <= p <= hi）。"""

    __slots__ = ("root", "lo_values", "hi_values", "size")

    def __init__(self, intervals: Sequence[tuple[int, int, int]]):
        """``intervals`` 为 ``(lo, hi, rule_seq)`` 序列；空序列得到空树。"""
        self.size = len(intervals)
        # 端点数组与规则下标一一对应，节点里只存下标，保证每条规则只存一份。
        self.lo_values = tuple(iv[0] for iv in intervals)
        self.hi_values = tuple(iv[1] for iv in intervals)
        self.root = self._build(list(intervals))

    # ------------------------------------------------------------------ build
    def _build(self, intervals: list[tuple[int, int, int]]) -> Optional[_Node]:
        if not intervals:
            return None
        # 取所有端点的中位数作中心：保证树高 O(log n)。
        endpoints: list[int] = []
        for lo, hi, _ in intervals:
            endpoints.append(lo)
            endpoints.append(hi)
        endpoints.sort()
        center = endpoints[len(endpoints) // 2]

        here: list[tuple[int, int, int]] = []
        left_side: list[tuple[int, int, int]] = []
        right_side: list[tuple[int, int, int]] = []
        for lo, hi, seq in intervals:
            if hi < center:
                left_side.append((lo, hi, seq))
            elif lo > center:
                right_side.append((lo, hi, seq))
            else:
                here.append((lo, hi, seq))

        by_lo = tuple(
            seq
            for _, _, seq in sorted(here, key=lambda iv: (iv[0], iv[2]))
        )
        by_hi = tuple(
            seq
            for _, _, seq in sorted(here, key=lambda iv: (-iv[1], iv[2]))
        )
        return _Node(
            center=center,
            by_lo=by_lo,
            by_hi=by_hi,
            left=self._build(left_side),
            right=self._build(right_side),
        )

    # ------------------------------------------------------------------ query
    def stab(self, point: int) -> frozenset[int]:
        """返回轴向上覆盖 ``point`` 的规则序号集合（不含防御性复核）。"""
        found: set[int] = set()
        node = self.root
        while node is not None:
            if point < node.center:
                # by_lo 按 lo 升序：lo <= point 的前缀都可能覆盖 point。
                los = node.by_lo
                lo_arr = self.lo_values
                cut = bisect_right(los, point, key=lambda seq: lo_arr[seq])
                found.update(los[:cut])
                node = node.left
            elif point > node.center:
                # by_hi 按 -hi 升序（即 hi 降序）：hi >= point 等价于
                # -hi <= -point，取排序后满足该条件的前缀。
                his = node.by_hi
                hi_arr = self.hi_values
                cut = bisect_right(his, -point, key=lambda seq: -hi_arr[seq])
                found.update(his[:cut])
                node = node.right
            else:
                # point == center：本节点所有区间都跨过 center，整段命中，
                # 子树区间与 center 不相交，无需继续下行。
                found.update(node.by_lo)
                break
        return frozenset(found)


class SpatialIndex:
    """水位 × 流量双轴区间索引：共享给首页单点查算与 ``/api/classify``。"""

    __slots__ = ("rules", "_wl_tree", "_fl_tree", "rule_count")

    def __init__(self, rules: Sequence[Rule]):
        # 以传入顺序（现行分区表的 ord 顺序）固定规则，序号即结果排序依据。
        self.rules: tuple[Rule, ...] = tuple(rules)
        self.rule_count = len(self.rules)
        wl_intervals = [
            (r.wl_min, r.wl_max, seq) for seq, r in enumerate(self.rules)
        ]
        fl_intervals = [
            (r.fl_min, r.fl_max, seq) for seq, r in enumerate(self.rules)
        ]
        self._wl_tree = _AxisIntervalTree(wl_intervals)
        self._fl_tree = _AxisIntervalTree(fl_intervals)

    def query(self, wl: int, fl: int) -> list[Rule]:
        """返回覆盖整数点 ``(wl, fl)`` 的全部规则，**按原顺序**排列。

        先在两轴分别刺查、取交集得到局部候选，再对候选逐条做闭区间复核
        （树的刺查本身已保证闭区间语义，复核只是防御性断言）。
        """
        wl_hits = self._wl_tree.stab(wl)
        fl_hits = self._fl_tree.stab(fl)
        candidate_seqs = wl_hits & fl_hits
        return [
            self.rules[seq]
            for seq in sorted(candidate_seqs)
            if self.rules[seq].covers(wl, fl)
        ]

    def query_stats(self, wl: int, fl: int) -> dict[str, int]:
        """查询同时返回候选计数，用于自测 / 展示“只检查局部候选”。"""
        wl_hits = self._wl_tree.stab(wl)
        fl_hits = self._fl_tree.stab(fl)
        union_seqs = wl_hits | fl_hits
        candidate_seqs = wl_hits & fl_hits
        checked = sum(1 for seq in candidate_seqs if self.rules[seq].covers(wl, fl))
        return {
            "rule_count": self.rule_count,
            "wl_axis_candidates": len(wl_hits),
            "fl_axis_candidates": len(fl_hits),
            "union_candidates": len(union_seqs),
            "intersection_candidates": len(candidate_seqs),
            "covers_checked": checked,
            "matched": checked,
        }
