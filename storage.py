import json
import os
from astrbot.core import sp

SP_PLAN_MAPPING = "afdian_model:plan_mapping"
SP_ACTIVE_UMOS = "afdian_model:active_umos"
SP_UMO_PREFIX = "afdian_model:umo:"
SP_BY_AFDIAN = "afdian_model:by_afdian:"

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, "data")
ORDERS_PATH = os.path.join(DATA_DIR, "processed_orders.json")


class StorageManager:
    def __init__(self, wire_fn=None):
        self._wire = wire_fn or print
        self._processed_orders = self._load_processed_orders()

    def _load_processed_orders(self) -> set:
        try:
            if os.path.exists(ORDERS_PATH):
                with open(ORDERS_PATH, "r", encoding="utf-8") as f:
                    return set(json.load(f))
        except Exception:
            pass
        return set()

    def _save_processed_orders(self):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(ORDERS_PATH, "w", encoding="utf-8") as f:
                json.dump(list(self._processed_orders), f)
        except Exception as e:
            self._wire(f"[AfdianModel] 保存订单记录失败: {e}", "error")

    def is_order_processed(self, order_no: str) -> bool:
        return order_no in self._processed_orders

    def mark_order_processed(self, order_no: str):
        self._processed_orders.add(order_no)
        self._save_processed_orders()

    def unmark_order_processed(self, order_no: str):
        self._processed_orders.discard(order_no)
        self._save_processed_orders()

    def clear_orders(self):
        self._processed_orders.clear()
        self._save_processed_orders()
        try:
            if os.path.exists(ORDERS_PATH):
                os.remove(ORDERS_PATH)
        except Exception:
            pass

    @staticmethod
    def _umo_key(umo) -> str:
        return f"{SP_UMO_PREFIX}{json.dumps(umo, separators=(',', ':'), sort_keys=True)}"

    def get_umo_data(self, umo) -> dict:
        key = self._umo_key(umo)
        data = sp.get(key, {})
        if data:
            data = dict(data)
            data["prefixes"] = self._str_to_list(data.get("prefixes", ""))
        return data

    def set_umo_data(self, umo, data: dict):
        key = self._umo_key(umo)
        sp.put(key, data)

    def register_umo(self, umo_key: str):
        active = sp.get(SP_ACTIVE_UMOS, [])
        if umo_key not in active:
            active.append(umo_key)
            sp.put(SP_ACTIVE_UMOS, active)

    def unregister_umo(self, umo_key: str):
        active = sp.get(SP_ACTIVE_UMOS, [])
        if umo_key in active:
            active.remove(umo_key)
            sp.put(SP_ACTIVE_UMOS, active)

    def get_active_umos(self) -> list:
        return sp.get(SP_ACTIVE_UMOS, [])

    def set_active_umos(self, active: list):
        sp.put(SP_ACTIVE_UMOS, active)

    def get_plan_mapping(self) -> dict:
        return sp.get(SP_PLAN_MAPPING, {})

    def set_plan_mapping(self, mapping: dict):
        sp.put(SP_PLAN_MAPPING, mapping)

    def get_user_mapping(self, user_id: str):
        return sp.get(f"{SP_BY_AFDIAN}{user_id}", None)

    def set_user_mapping(self, user_id: str, umo_key: str):
        sp.put(f"{SP_BY_AFDIAN}{user_id}", umo_key)

    def remove_user_mapping(self, user_id: str):
        sp.put(f"{SP_BY_AFDIAN}{user_id}", None)

    def get_current_model(self, umo) -> str:
        return sp.get(self._umo_key(umo) + ":current", "默认模型")

    def set_current_model(self, umo, model: str):
        sp.put(self._umo_key(umo) + ":current", model)

    @staticmethod
    def _list_to_str(lst: list) -> str:
        if not lst:
            return ""
        return ",".join(lst)

    @staticmethod
    def _str_to_list(s: str) -> list:
        if not s or not isinstance(s, str):
            return []
        return [item.strip() for item in s.split(",") if item.strip()]
