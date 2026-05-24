"""持久化存储 —— 订单去重 + sp 状态管理 + persistence.json 崩溃恢复。

所有文件 I/O 经由 utils.atomic_write_json / safe_read_json，确保原子性和一致性。
"""

import os
import threading
from typing import Any

from astrbot.core import sp

from .utils import (
    atomic_write_json,
    list_to_str,
    log_msg,
    safe_read_json,
    str_to_list,
)

# ── sp 键常量 ─────────────────────────────────────
SP_PLAN_MAPPING = "afdian_model:plan_mapping"
SP_ACTIVE_UMOS  = "afdian_model:active_umos"
SP_UMO_PREFIX   = "afdian_model:umo:"
SP_BY_AFDIAN    = "afdian_model:by_afdian:"
SP_USER_INDEX   = "afdian_model:user_index"


class StorageManager:
    """统一持久化管理器。

    - sp (SharedPreferences) 负责运行时状态
    - persistence.json 负责崩溃恢复快照
    - processed_orders.json 负责订单去重
    """

    # 静态工具方法（供 PlanManager 等外部模块复用）
    _list_to_str = staticmethod(list_to_str)
    _str_to_list = staticmethod(str_to_list)

    def __init__(self, data_dir: str, wire_fn=None) -> None:
        self._wire = wire_fn
        self._data_dir = data_dir
        self._orders_path = os.path.join(data_dir, "processed_orders.json")
        self._persistence_path = os.path.join(data_dir, "persistence.json")
        self._processed_orders: set[str] = self._load_processed_orders()
        self._lock = threading.Lock()
        self._batch_depth = 0

    # ── 订单去重 ──────────────────────────────────

    def is_order_processed(self, order_no: str) -> bool:
        return order_no in self._processed_orders

    def mark_order_processed(self, order_no: str) -> None:
        with self._lock:
            self._processed_orders.add(order_no)
            self._flush_orders()

    def unmark_order_processed(self, order_no: str) -> None:
        with self._lock:
            self._processed_orders.discard(order_no)
            self._flush_orders()

    def clear_orders(self) -> None:
        with self._lock:
            self._processed_orders.clear()
            self._flush_orders()
            try:
                os.remove(self._orders_path)
            except FileNotFoundError:
                pass

    # ── sp 数据读写 ───────────────────────────────

    @staticmethod
    def _umo_key(umo) -> str:
        import json
        return f"{SP_UMO_PREFIX}{json.dumps(umo, separators=(',', ':'), sort_keys=True)}"

    def get_umo_data(self, umo) -> dict[str, Any]:
        data = sp.get(self._umo_key(umo), {}) or {}
        if data:
            data = dict(data)
            data["prefixes"] = str_to_list(data.get("prefixes", ""))
        return data

    def set_umo_data(self, umo, data: dict) -> None:
        sp.put(self._umo_key(umo), data)
        self._maybe_dump()

    def get_umo_data_by_key(self, umo_key: str) -> dict[str, Any]:
        data = sp.get(umo_key, {}) or {}
        if data:
            data = dict(data)
            data["prefixes"] = str_to_list(data.get("prefixes", ""))
        return data

    def set_umo_data_by_key(self, umo_key: str, data: dict) -> None:
        sp.put(umo_key, data)
        self._maybe_dump()

    def remove_umo_by_key(self, umo_key: str) -> None:
        sp.put(umo_key, None)
        sp.put(umo_key + ":current", None)
        self._maybe_dump()

    def register_umo(self, umo_key: str) -> None:
        active = sp.get(SP_ACTIVE_UMOS, [])
        if umo_key not in active:
            active.append(umo_key)
            sp.put(SP_ACTIVE_UMOS, active)
        self._maybe_dump()

    def unregister_umo(self, umo_key: str) -> None:
        active = sp.get(SP_ACTIVE_UMOS, [])
        if umo_key in active:
            active.remove(umo_key)
            sp.put(SP_ACTIVE_UMOS, active)
        self._maybe_dump()

    def get_active_umos(self) -> list:
        return sp.get(SP_ACTIVE_UMOS, [])

    def set_active_umos(self, active: list) -> None:
        sp.put(SP_ACTIVE_UMOS, active)
        self._maybe_dump()

    def get_plan_mapping(self) -> dict:
        return sp.get(SP_PLAN_MAPPING, {})

    def set_plan_mapping(self, mapping: dict) -> None:
        sp.put(SP_PLAN_MAPPING, mapping)
        self._maybe_dump()

    def get_user_mapping(self, user_id: str) -> str | None:
        return sp.get(f"{SP_BY_AFDIAN}{user_id}", None)

    def set_user_mapping(self, user_id: str, umo_key: str) -> None:
        sp.put(f"{SP_BY_AFDIAN}{user_id}", umo_key)
        self._register_user_id(user_id)
        self._maybe_dump()

    def remove_user_mapping(self, user_id: str) -> None:
        sp.put(f"{SP_BY_AFDIAN}{user_id}", None)
        self._unregister_user_id(user_id)
        self._maybe_dump()

    @staticmethod
    def _default_model() -> str:
        return sp.get("curr_provider", "", scope="global", scope_id="global") or "默认模型"

    def get_current_model(self, umo) -> str:
        return sp.get(self._umo_key(umo) + ":current", self._default_model())

    def set_current_model(self, umo, model: str) -> None:
        sp.put(self._umo_key(umo) + ":current", model)
        self._maybe_dump()

    def set_current_model_by_key(self, umo_key: str, model: str) -> None:
        sp.put(umo_key + ":current", model)
        self._maybe_dump()

    # ── 批量写入 ──────────────────────────────────

    def begin_batch(self) -> None:
        self._batch_depth += 1

    def end_batch(self) -> None:
        if self._batch_depth > 0:
            self._batch_depth -= 1
        if self._batch_depth == 0:
            self._dump_state()

    # ── 全量重置 ──────────────────────────────────

    def full_reset(self) -> dict:
        """清除所有持久化数据，保留插件配置。返回统计信息。"""
        stats = {"orders": 0, "umo_data": 0, "user_mappings": 0, "active_umos": 0}
        stats["orders"] = len(self._processed_orders)
        self.clear_orders()

        active = list(sp.get(SP_ACTIVE_UMOS, []))
        stats["active_umos"] = len(active)
        seen: set[str] = set()

        for umo_key in active:
            sp.put(umo_key, None)
            sp.put(umo_key + ":current", None)
            seen.add(umo_key)
            stats["umo_data"] += 1

        for user_id in sp.get(SP_USER_INDEX, []):
            umo_key = sp.get(f"{SP_BY_AFDIAN}{user_id}", None)
            if umo_key and umo_key not in seen:
                sp.put(umo_key, None)
                sp.put(umo_key + ":current", None)
                stats["umo_data"] += 1
            sp.put(f"{SP_BY_AFDIAN}{user_id}", None)
            stats["user_mappings"] += 1

        sp.put(SP_ACTIVE_UMOS, [])
        sp.put(SP_PLAN_MAPPING, {})
        sp.put(SP_USER_INDEX, [])

        try:
            os.remove(self._persistence_path)
        except FileNotFoundError:
            pass

        log_msg(self._wire, f"全量重置完成: {stats}")
        return stats

    # ── 旧数据迁移 ────────────────────────────────

    @staticmethod
    def migrate_umo_data(data: dict, wire_fn=None) -> dict:
        """迁移旧格式到 Lv1/Lv2 分级存储。幂等。"""
        if not isinstance(data, dict) or "l1_days" in data:
            return data
        old_level = data.get("level", "1")
        old_days = data.get("remaining_days", 0)
        data["l1_days"] = 0 if old_level == "2" else old_days
        data["l2_days"] = old_days if old_level == "2" else 0
        data["active_level"] = old_level
        data["remaining_days"] = data["l1_days"] + data["l2_days"]
        log_msg(wire_fn, f"数据迁移: lv={old_level} days={old_days} -> l1={data['l1_days']} l2={data['l2_days']}")
        return data

    # ── 内部方法 ──────────────────────────────────

    def _maybe_dump(self) -> None:
        """batch 模式下跳过，否则立即持久化。"""
        if not self._batch_depth:
            self._dump_state()

    def _load_processed_orders(self) -> set[str]:
        data = safe_read_json(self._orders_path, [])
        return set(data) if isinstance(data, list) else set()

    def _flush_orders(self) -> None:
        """调用方已持有 _lock。"""
        atomic_write_json(self._orders_path, list(self._processed_orders))

    def _register_user_id(self, user_id: str) -> None:
        idx = sp.get(SP_USER_INDEX, [])
        if user_id not in idx:
            idx.append(user_id)
            sp.put(SP_USER_INDEX, idx)

    def _unregister_user_id(self, user_id: str) -> None:
        idx = sp.get(SP_USER_INDEX, [])
        if user_id in idx:
            idx.remove(user_id)
            sp.put(SP_USER_INDEX, idx)

    def _dump_state(self) -> None:
        """全量快照到 persistence.json。"""
        with self._lock:
            try:
                state = {
                    "plan_mapping": sp.get(SP_PLAN_MAPPING, {}),
                    "active_umos": sp.get(SP_ACTIVE_UMOS, []),
                    "umo_data": {},
                    "user_mappings": {},
                    "user_index": sp.get(SP_USER_INDEX, []),
                }
                for umo_key in state["active_umos"]:
                    data = sp.get(umo_key, {}) or {}
                    if data:
                        state["umo_data"][umo_key] = dict(data)
                    current = sp.get(umo_key + ":current", "")
                    if current:
                        state["umo_data"][umo_key + ":current"] = current
                for user_id in state["user_index"]:
                    val = sp.get(f"{SP_BY_AFDIAN}{user_id}", None)
                    if val is not None:
                        state["user_mappings"][user_id] = val
                atomic_write_json(self._persistence_path, state)
            except OSError as e:
                log_msg(self._wire, f"状态持久化失败: {e}", "error")

    def restore_state(self) -> bool:
        """从 persistence.json 恢复 sp 状态。"""
        state = safe_read_json(self._persistence_path)
        if not state:
            log_msg(self._wire, "无持久化文件，跳过恢复")
            return False

        if state.get("plan_mapping"):
            sp.put(SP_PLAN_MAPPING, state["plan_mapping"])
        if state.get("active_umos"):
            sp.put(SP_ACTIVE_UMOS, state["active_umos"])
        if state.get("user_index"):
            sp.put(SP_USER_INDEX, state["user_index"])
        for key, value in state.get("umo_data", {}).items():
            if value:
                sp.put(key, value)
        for user_id, umo_key in state.get("user_mappings", {}).items():
            sp.put(f"{SP_BY_AFDIAN}{user_id}", umo_key)

        active_count = len(state.get("active_umos", []))
        log_msg(self._wire, f"状态从持久化文件恢复: {active_count} 个活跃绑定")
        return True

    def persist(self) -> None:
        """公开持久化入口。"""
        self._dump_state()
