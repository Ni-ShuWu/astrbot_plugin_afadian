import json
from datetime import datetime, timedelta
from astrbot.core import sp
from .storage import StorageManager
from .plan_manager import PlanManager

SP_ACTIVE_UMOS = "afdian_model:active_umos"
SP_UMO_PREFIX = "afdian_model:umo:"
SP_BY_AFDIAN = "afdian_model:by_afdian:"


class UserManager:
    def __init__(self, storage: StorageManager, plan_manager: PlanManager, wire_fn=None):
        self._storage = storage
        self._plan_manager = plan_manager
        self._wire = wire_fn or print

    async def bind_user(self, user_id: str, plan_id: str, plan: dict, umo, create_time: int = 0, order_no: str = ""):
        days = plan["days"]
        prefixes = plan["prefixes"]
        existing = sp.get(f"{SP_BY_AFDIAN}{user_id}", None)
        umo_key = self._storage._umo_key(umo)
        
        if existing and existing != umo_key:
            old_data = sp.get(existing, {})
            if old_data:
                new_data = sp.get(umo_key, {})
                if not new_data:
                    new_data = {
                        "remaining_days": old_data.get("remaining_days", 0),
                        "prefixes": old_data.get("prefixes", ""),
                        "expire_time": old_data.get("expire_time", ""),
                        "plan_id": old_data.get("plan_id", ""),
                        "order_time": old_data.get("order_time", ""),
                        "used_orders": old_data.get("used_orders", [])
                    }
                self._storage.set_umo_data(umo, new_data)
                sp.put(existing, None)
                self._storage.unregister_umo(existing)

        umo_data = self._storage.get_umo_data(umo)
        order_time = datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M:%S") if create_time else "未知"

        if umo_data:
            used_orders = umo_data.get("used_orders", [])
            if order_no and order_no in used_orders:
                self._wire(f"[AfdianModel] 订单{order_no}已在用户数据中，跳过绑定")
                return umo_data

            umo_data["remaining_days"] += days
            existing_prefixes = self._storage._str_to_list(umo_data.get("prefixes", ""))
            combined_prefixes = list(set(existing_prefixes + prefixes))
            umo_data["prefixes"] = self._storage._list_to_str(combined_prefixes)
        else:
            umo_data = {
                "remaining_days": days, 
                "prefixes": self._storage._list_to_str(prefixes), 
                "plan_id": plan_id, 
                "used_orders": []
            }

        if order_no:
            used_orders = umo_data.get("used_orders", [])
            if order_no not in used_orders:
                used_orders.append(order_no)
                umo_data["used_orders"] = used_orders

        umo_data["order_time"] = order_time
        umo_data["expire_time"] = (datetime.now() + timedelta(days=umo_data["remaining_days"])).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self._storage.set_umo_data(umo, umo_data)
        self._storage.register_umo(umo_key)
        sp.put(f"{SP_BY_AFDIAN}{user_id}", umo_key)
        return umo_data

    def get_model_list(self, config_fn) -> list:
        model_list_raw = config_fn().get("model_list", "")
        model_list = self._plan_manager._parse_models(model_list_raw)
        self._wire(f"[AfdianModel] _get_model_list: raw='{model_list_raw}' parsed={model_list}", "info")
        return model_list

    def has_model_permission(self, umo_data, model_name: str) -> tuple[bool, list]:
        if not umo_data:
            return False, []
        prefixes = umo_data.get("prefixes", [])
        has_permission = False
        for p in prefixes:
            if model_name.startswith(p) or p.startswith(model_name) or model_name == p:
                has_permission = True
                break
        return has_permission, prefixes
