"""持久化存储 —— 完全基于 AstrBot 官方 sp 接口。

不再维护任何自定义 JSON 文件。所有数据通过 sp (SharedPreferences/SQLite) 持久化。
"""

from typing import Any

from astrbot.core import sp

from .utils import list_to_str, log_msg, str_to_list

# ── sp 键常量 ─────────────────────────────────────
SP_PLAN_MAPPING      = "afdian_model:plan_mapping"
SP_ACTIVE_UMOS       = "afdian_model:active_umos"
SP_UMO_PREFIX        = "afdian_model:umo:"
SP_BY_AFDIAN         = "afdian_model:by_afdian:"
SP_USER_INDEX        = "afdian_model:user_index"
SP_PROCESSED_ORDERS  = "afdian_model:processed_orders"


class StorageManager:
    """统一持久化管理器，完全基于 AstrBot sp。"""

    _list_to_str = staticmethod(list_to_str)
    _str_to_list = staticmethod(str_to_list)

    def __init__(self, wire_fn=None) -> None:
        self._wire = wire_fn

    # ── 订单去重 ──────────────────────────────────

    def is_order_processed(self, order_no: str) -> bool:
        return order_no in self._get_orders()

    def mark_order_processed(self, order_no: str) -> None:
        orders = self._get_orders()
        orders.add(order_no)
        sp.put(SP_PROCESSED_ORDERS, list(orders))

    def unmark_order_processed(self, order_no: str) -> None:
        orders = self._get_orders()
        orders.discard(order_no)
        sp.put(SP_PROCESSED_ORDERS, list(orders))

    def clear_orders(self) -> None:
        sp.put(SP_PROCESSED_ORDERS, [])

    def _get_orders(self) -> set[str]:
        raw = sp.get(SP_PROCESSED_ORDERS, [])
        return set(raw) if isinstance(raw, list) else set()

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

    def get_umo_data_by_key(self, umo_key: str) -> dict[str, Any]:
        data = sp.get(umo_key, {}) or {}
        if data:
            data = dict(data)
            data["prefixes"] = str_to_list(data.get("prefixes", ""))
        return data

    def set_umo_data_by_key(self, umo_key: str, data: dict) -> None:
        sp.put(umo_key, data)

    def remove_umo_by_key(self, umo_key: str) -> None:
        sp.put(umo_key, None)
        sp.put(umo_key + ":current", None)

    def register_umo(self, umo_key: str) -> None:
        active = sp.get(SP_ACTIVE_UMOS, [])
        if umo_key not in active:
            active.append(umo_key)
            sp.put(SP_ACTIVE_UMOS, active)

    def unregister_umo(self, umo_key: str) -> None:
        active = sp.get(SP_ACTIVE_UMOS, [])
        if umo_key in active:
            active.remove(umo_key)
            sp.put(SP_ACTIVE_UMOS, active)

    def get_active_umos(self) -> list:
        return sp.get(SP_ACTIVE_UMOS, [])

    def set_active_umos(self, active: list) -> None:
        sp.put(SP_ACTIVE_UMOS, active)

    def get_plan_mapping(self) -> dict:
        return sp.get(SP_PLAN_MAPPING, {})

    def set_plan_mapping(self, mapping: dict) -> None:
        sp.put(SP_PLAN_MAPPING, mapping)

    def get_user_mapping(self, user_id: str) -> str | None:
        return sp.get(f"{SP_BY_AFDIAN}{user_id}", None)

    def set_user_mapping(self, user_id: str, umo_key: str) -> None:
        sp.put(f"{SP_BY_AFDIAN}{user_id}", umo_key)
        self._register_user_id(user_id)

    def remove_user_mapping(self, user_id: str) -> None:
        sp.put(f"{SP_BY_AFDIAN}{user_id}", None)
        self._unregister_user_id(user_id)

    @staticmethod
    def _default_model() -> str:
        return sp.get("curr_provider", "", scope="global", scope_id="global") or "默认模型"

    def get_current_model(self, umo) -> str:
        return sp.get(self._umo_key(umo) + ":current", self._default_model())

    def set_current_model(self, umo, model: str) -> None:
        sp.put(self._umo_key(umo) + ":current", model)

    def set_current_model_by_key(self, umo_key: str, model: str) -> None:
        sp.put(umo_key + ":current", model)

    # ── 全量重置 ──────────────────────────────────

    def full_reset(self) -> dict:
        """清除所有持久化数据。返回统计信息。"""
        stats: dict[str, int] = {"orders": 0, "umo_data": 0, "user_mappings": 0, "active_umos": 0}

        orders = self._get_orders()
        stats["orders"] = len(orders)
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

    # ── 内部 ──────────────────────────────────────

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
