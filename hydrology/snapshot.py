"""查算快照：进程内不可变的“量程 + 分区表 + 空间索引”版本缓存。

快照语义（对应发布台的口径一致性要求）
--------------------------------------
* 一次查算**开始时**先读现行的两个版本指针（``current_range_id`` /
  ``current_rules_id``），随后整次查算只使用这一对版本构成的不可变快照；
* 发布成功只移动数据库里的指针：**新查算**随即取到新版快照，而已经在途的
  查算仍持有旧快照对象，按原口径算完，不会读到半新半旧的混合版本；
* 发布被驳回时指针不动，所有进程继续命中旧快照，行为与发布前完全一致；
* gunicorn 的两个 worker 是独立进程，各自维护一份本缓存：任一进程在查算
  开始时读到哪个指针版本，就构建 / 复用哪个版本的快照，天然满足
  “两个服务进程都用查算开始时已生效的版本”。

缓存只按 ``(range_id, rules_id)`` 保留最近若干个不可变快照。空间上，每个
快照持有的规则与双轴索引都随规则条数 **线性增长**；被淘汰的旧快照若仍被
在途查算引用，会作为普通对象存活到该次查算结束，之后自动回收。
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Optional

from .db import load_versioned, read_current_ids
from .domain import Range, Rule, classify_rules
from .index import SpatialIndex

# 每个进程最多缓存多少个历史版本快照（仅用于平滑发布瞬间的在途查算）。
MAX_CACHED_SNAPSHOTS = 4


@dataclass(frozen=True)
class Snapshot:
    """一次查算所依据的完整口径；构建后不再变化，可跨线程安全共享。"""

    range_id: Optional[int]
    rules_id: Optional[int]
    range: Optional[Range]
    rules: tuple[Rule, ...]
    range_meta: Optional[dict[str, Any]]
    rules_version_meta: Optional[dict[str, Any]]
    index: SpatialIndex

    @property
    def key(self) -> tuple[Optional[int], Optional[int]]:
        return (self.range_id, self.rules_id)

    def classify(self, wl: int, fl: int) -> dict[str, Any]:
        """用本快照的空间索引做单点查算（闭区间 + 优先级，原序返回命中）。"""
        return classify_rules(self.range, self.rules, wl, fl, index=self.index)


class SnapshotStore:
    """进程内、线程安全的版本化快照缓存（每个 gunicorn worker 各持一份）。"""

    def __init__(self, max_size: int = MAX_CACHED_SNAPSHOTS) -> None:
        self._lock = threading.Lock()
        self._cache: dict[tuple[Optional[int], Optional[int]], Snapshot] = {}
        self._order: list[tuple[Optional[int], Optional[int]]] = []
        self._max_size = max_size

    def current(self, conn: sqlite3.Connection) -> Optional[Snapshot]:
        """按查算开始时的现行指针取快照；指针未初始化时返回 ``None``。"""
        range_id, rules_id = read_current_ids(conn)
        if range_id is None or rules_id is None:
            return None

        key = (range_id, rules_id)
        with self._lock:
            snapshot = self._cache.get(key)
        if snapshot is not None:
            return snapshot

        # 事务外按固定版本号读取：即便读期间指针被发布事务移动，取到的仍是
        # 本次查算开始时那一对版本（WAL 下读不阻塞写、写也不阻塞读）。
        loaded = load_versioned(conn, range_id, rules_id)
        snapshot = Snapshot(
            range_id=range_id,
            rules_id=rules_id,
            range=loaded["range"],
            rules=tuple(loaded["rules"]),
            range_meta=loaded["range_meta"],
            rules_version_meta=loaded["rules_version_meta"],
            index=SpatialIndex(loaded["rules"]),
        )
        self._store(key, snapshot)
        return snapshot

    def _store(self, key: tuple[Optional[int], Optional[int]], snapshot: Snapshot) -> None:
        with self._lock:
            if key in self._cache:
                return
            self._cache[key] = snapshot
            self._order.append(key)
            while len(self._order) > self._max_size:
                old_key = self._order.pop(0)
                # 正在被在途查算引用的快照不会因弹出而失效：调用方仍持有对象。
                self._cache.pop(old_key, None)

    def clear(self) -> None:
        """主要供测试使用：丢弃全部进程内缓存。"""
        with self._lock:
            self._cache.clear()
            self._order.clear()


def get_snapshot_store() -> SnapshotStore:
    """请求上下文中的进程级缓存（挂在 flask app.extensions 上，每进程一份）。"""
    from flask import current_app

    store = current_app.extensions.get("hydrology_snapshot_store")
    if store is None:
        store = SnapshotStore()
        current_app.extensions["hydrology_snapshot_store"] = store
    return store
