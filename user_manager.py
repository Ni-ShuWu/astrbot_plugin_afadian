"""用户管理 —— 绑定、权限检查、模型列表。"""

from datetime import datetime, timedelta

from astrbot.core import sp

from .plan_manager import PlanManager
from .storage import StorageManager
from .utils import list_to_str, log_msg, model_names_from_config, str_to_list


class UserManager:
    """用户赞助绑定与权限管理器。"""

    def __init__(self, storage: StorageManager, plan_manager: PlanManager, wire_fn=None) -> None:
        self._storage = storage
        self._plan_manager = plan_manager
        self._wire = wire_fn

    async def bind_user(
        self, user_id: str, plan_id: str, plan: dict, umo, create_time: int = 0, order_no: str = ""
    ) -> dict:
        """绑定用户到赞助方案，支持累加。"""
        days = plan["days"]
        prefixes = plan["prefixes"]
        level = plan.get("level", "1")
        existing = self._storage.get_user_mapping(user_id)
        umo_key = self._storage._umo_key(umo)

        # 迁移旧绑定
        if existing and existing != umo_key:
            old_data = sp.get(existing, {}) or {}
            if old_data:
                old_data = StorageManager.migrate_umo_data(old_data, self._wire)
                new_data = sp.get(umo_key, {}) or {}
                if not new_data:
                    new_data = dict(old_data)
                self._storage.set_umo_data(umo, new_data)
                self._storage.remove_umo_by_key(existing)
                self._storage.unregister_umo(existing)

        umo_data = self._storage.get_umo_data(umo)
        order_time = datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M:%S") if create_time else "未知"

        if umo_data:
            umo_data = StorageManager.migrate_umo_data(umo_data, self._wire)
            used_orders = umo_data.get("used_orders", [])
            if order_no and order_no in used_orders:
                return umo_data

            if level == "2":
                umo_data["l2_days"] = umo_data.get("l2_days", 0) + days
                umo_data["active_level"] = "2"
            else:
                umo_data["l1_days"] = umo_data.get("l1_days", 0) + days
                if umo_data.get("active_level", "0") != "2":
                    umo_data["active_level"] = "1"

            umo_data["remaining_days"] = umo_data.get("l1_days", 0) + umo_data.get("l2_days", 0)
            existing_pf = str_to_list(umo_data.get("prefixes", ""))
            combined = list(set(existing_pf + prefixes))
            umo_data["prefixes"] = list_to_str(combined)
            umo_data["expire_time"] = (datetime.now() + timedelta(days=umo_data["remaining_days"])).strftime("%Y-%m-%d %H:%M:%S")
            umo_data["level"] = "2" if umo_data.get("l2_days", 0) > 0 else "1"
            if order_no:
                used_orders.append(order_no)
                umo_data["used_orders"] = used_orders
        else:
            umo_data = {
                "remaining_days": days,
                "prefixes": list_to_str(prefixes),
                "order_time": order_time,
                "expire_time": (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
                "plan_id": plan_id,
                "used_orders": [order_no] if order_no else [],
                "level": level,
                "active_level": level,
                "l1_days": days if level == "1" else 0,
                "l2_days": days if level == "2" else 0,
            }

        umo_data["order_time"] = order_time
        umo_data["expire_time"] = (datetime.now() + timedelta(days=umo_data["remaining_days"])).strftime("%Y-%m-%d %H:%M:%S")
        self._storage.set_umo_data(umo, umo_data)
        self._storage.register_umo(umo_key)
        self._storage.set_user_mapping(user_id, umo_key)
        return umo_data

    def get_model_list(self, config_fn) -> list[str]:
        return model_names_from_config(config_fn().get("model_list", ""))

    def has_model_permission(self, umo_data: dict | None, model_name: str) -> tuple[bool, list[str]]:
        """检查用户是否有模型权限。高级用户可用低级模型：Lv2 > Lv1 > Lv0。"""
        if not umo_data:
            return False, []

        user_prefixes = umo_data.get("prefixes", [])
        if isinstance(user_prefixes, str):
            user_prefixes = str_to_list(user_prefixes)

        for p in user_prefixes:
            if model_name.startswith(p) or p.startswith(model_name) or model_name == p:
                return True, user_prefixes

        user_level = umo_data.get("active_level", umo_data.get("level", "1"))

        if user_level == "2":
            level_1_pf = self._get_level_prefixes("1")
            for p in level_1_pf:
                if model_name.startswith(p) or p.startswith(model_name) or model_name == p:
                    return True, list(set(user_prefixes + level_1_pf))

        if user_level in ("1", "2"):
            level_0_pf = self._get_level_prefixes("0")
            for p in level_0_pf:
                if model_name.startswith(p) or p.startswith(model_name) or model_name == p:
                    return True, list(set(user_prefixes + level_0_pf))

        if user_level == "0":
            level_0_pf = self._get_level_prefixes("0")
            for p in level_0_pf:
                if model_name.startswith(p) or p.startswith(model_name) or model_name == p:
                    return True, list(set(user_prefixes + level_0_pf))

        return False, user_prefixes

    def _get_level_prefixes(self, level: str) -> list[str]:
        config_fn = getattr(self._plan_manager, "_config_fn", None)
        if not callable(config_fn):
            return []
        cfg = config_fn()
        key = {"0": "model_list", "1": "models_1", "2": "models_2"}.get(level, "")
        return model_names_from_config(cfg.get(key, "")) if key else []
