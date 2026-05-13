import json
from datetime import datetime, timedelta
from astrbot.core import sp
from .storage import StorageManager
from .plan_manager import PlanManager


class UserManager:
    def __init__(self, storage: StorageManager, plan_manager: PlanManager, wire_fn=None):
        self._storage = storage
        self._plan_manager = plan_manager
        self._wire = wire_fn or print

    async def bind_user(self, user_id: str, plan_id: str, plan: dict, umo, create_time: int = 0, order_no: str = ""):
        days = plan["days"]
        prefixes = plan["prefixes"]
        existing = self._storage.get_user_mapping(user_id)
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
                self._storage.remove_umo_by_key(existing)
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
            current_level = umo_data.get("level", "1")
            new_level = plan.get("level", "1")
            if (new_level == "2" or (new_level == "1" and current_level == "2")):
                umo_data["level"] = new_level
        else:
            umo_data = {
                "remaining_days": days, 
                "prefixes": self._storage._list_to_str(prefixes), 
                "plan_id": plan_id, 
                "level": plan.get("level", "1"),
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
        self._storage.set_user_mapping(user_id, umo_key)
        return umo_data

    def get_model_list(self, config_fn) -> list:
        model_list_raw = config_fn().get("model_list", "")
        model_list = self._plan_manager._parse_models(model_list_raw)
        self._wire(f"[AfdianModel] _get_model_list: raw='{model_list_raw}' parsed={model_list}", "info")
        return model_list

    def has_model_permission(self, umo_data, model_name: str) -> tuple[bool, list]:
        if not umo_data:
            return False, []

        user_prefixes = umo_data.get("prefixes", [])
        for p in user_prefixes:
            if model_name.startswith(p) or p.startswith(model_name) or model_name == p:
                return True, user_prefixes

        user_level = umo_data.get("level", "1")
        if user_level == "2":
            level_1_prefixes = self._get_level_1_prefixes()
            if level_1_prefixes:
                for p in level_1_prefixes:
                    if model_name.startswith(p) or p.startswith(model_name) or model_name == p:
                        combined = list(set(user_prefixes + level_1_prefixes))
                        return True, combined

        return False, user_prefixes
    
    def _get_level_1_prefixes(self) -> list:
        try:
            config_fn = getattr(self._plan_manager, '_config_fn', None)
            if config_fn and callable(config_fn):
                cfg = config_fn()
                self._wire(f"[AfdianModel] _get_level_1_prefixes 读取配置: models_1={cfg.get('models_1', '')}", "info")
                level_1_prefixes = self._plan_manager._parse_models(cfg.get("models_1", ""))
                return level_1_prefixes
            else:
                self._wire("[AfdianModel] _get_level_1_prefixes: config_fn 不可用", "warning")
        except Exception as e:
            self._wire(f"[AfdianModel] _get_level_1_prefixes 获取Lv1模型失败: {e}", "error")
        return []
