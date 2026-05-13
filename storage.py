import json
import os
from astrbot.core import sp

SP_PLAN_MAPPING = "afdian_model:plan_mapping"
SP_ACTIVE_UMOS = "afdian_model:active_umos"
SP_UMO_PREFIX = "afdian_model:umo:"
SP_BY_AFDIAN = "afdian_model:by_afdian:"
SP_USER_INDEX = "afdian_model:user_index"

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, "data")
ORDERS_PATH = os.path.join(DATA_DIR, "processed_orders.json")
PERSISTENCE_PATH = os.path.join(DATA_DIR, "persistence.json")


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
        self._dump_state()

    def register_umo(self, umo_key: str):
        active = sp.get(SP_ACTIVE_UMOS, [])
        if umo_key not in active:
            active.append(umo_key)
            sp.put(SP_ACTIVE_UMOS, active)
        self._dump_state()

    def unregister_umo(self, umo_key: str):
        active = sp.get(SP_ACTIVE_UMOS, [])
        if umo_key in active:
            active.remove(umo_key)
            sp.put(SP_ACTIVE_UMOS, active)
        self._dump_state()

    def get_active_umos(self) -> list:
        return sp.get(SP_ACTIVE_UMOS, [])

    def set_active_umos(self, active: list):
        sp.put(SP_ACTIVE_UMOS, active)
        self._dump_state()

    def get_plan_mapping(self) -> dict:
        return sp.get(SP_PLAN_MAPPING, {})

    def set_plan_mapping(self, mapping: dict):
        sp.put(SP_PLAN_MAPPING, mapping)
        self._dump_state()

    def get_user_mapping(self, user_id: str):
        return sp.get(f"{SP_BY_AFDIAN}{user_id}", None)

    def set_user_mapping(self, user_id: str, umo_key: str):
        sp.put(f"{SP_BY_AFDIAN}{user_id}", umo_key)
        self._register_user_id(user_id)
        self._dump_state()

    def remove_user_mapping(self, user_id: str):
        sp.put(f"{SP_BY_AFDIAN}{user_id}", None)
        self._unregister_user_id(user_id)
        self._dump_state()

    def get_current_model(self, umo) -> str:
        return sp.get(self._umo_key(umo) + ":current", "默认模型")

    def set_current_model(self, umo, model: str):
        sp.put(self._umo_key(umo) + ":current", model)
        self._dump_state()

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

    # ── 持久化层 ──

    def _register_user_id(self, user_id: str):
        """记录 user_id 以便 dump_state 时能枚举 user_mappings"""
        idx = sp.get(SP_USER_INDEX, [])
        if user_id not in idx:
            idx.append(user_id)
            sp.put(SP_USER_INDEX, idx)

    def _unregister_user_id(self, user_id: str):
        idx = sp.get(SP_USER_INDEX, [])
        if user_id in idx:
            idx.remove(user_id)
            sp.put(SP_USER_INDEX, idx)

    def _dump_state(self):
        """将所有 sp 状态写入 persistence.json"""
        try:
            state = {
                "plan_mapping": sp.get(SP_PLAN_MAPPING, {}),
                "active_umos": sp.get(SP_ACTIVE_UMOS, []),
                "umo_data": {},
                "user_mappings": {},
                "user_index": sp.get(SP_USER_INDEX, []),
            }
            for umo_key in state["active_umos"]:
                data = sp.get(umo_key, {})
                if data:
                    state["umo_data"][umo_key] = dict(data) if isinstance(data, dict) else data
                current = sp.get(umo_key + ":current", "")
                if current:
                    state["umo_data"][umo_key + ":current"] = current
            for user_id in state["user_index"]:
                val = sp.get(f"{SP_BY_AFDIAN}{user_id}", None)
                if val is not None:
                    state["user_mappings"][user_id] = val

            os.makedirs(DATA_DIR, exist_ok=True)
            with open(PERSISTENCE_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._wire(f"[AfdianModel] 状态持久化失败: {e}", "error")

    def restore_state(self):
        """从 persistence.json 恢复所有 sp 状态（插件重载后调用）"""
        try:
            if not os.path.exists(PERSISTENCE_PATH):
                self._wire("[AfdianModel] 无持久化文件，跳过恢复", "info")
                return False
            with open(PERSISTENCE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)

            restored = 0
            if state.get("plan_mapping"):
                sp.put(SP_PLAN_MAPPING, state["plan_mapping"])
                restored += 1
            if state.get("active_umos"):
                sp.put(SP_ACTIVE_UMOS, state["active_umos"])
                restored += 1
            if state.get("user_index"):
                sp.put(SP_USER_INDEX, state["user_index"])
            for key, value in state.get("umo_data", {}).items():
                if value:
                    sp.put(key, value)
            for user_id, umo_key in state.get("user_mappings", {}).items():
                sp.put(f"{SP_BY_AFDIAN}{user_id}", umo_key)

            active_count = len(state.get("active_umos", []))
            self._wire(
                f"[AfdianModel] ✅ 状态从持久化文件恢复: {active_count} 个活跃绑定",
                "info"
            )
            return True
        except Exception as e:
            self._wire(f"[AfdianModel] 状态恢复失败: {e}", "error")
            return False

    def persist(self):
        """公开的持久化入口，供直接 sp.put 后调用"""
        self._dump_state()
