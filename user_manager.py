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

    def _migrate_umo_data(self, umo_data: dict) -> dict:
        """迁移旧数据到新的分级存储格式，委托给 StorageManager 统一实现"""
        return StorageManager.migrate_umo_data(umo_data, self._wire)

    async def bind_user(self, user_id: str, plan_id: str, plan: dict, umo, create_time: int = 0, order_no: str = ""):
        days = plan["days"]
        prefixes = plan["prefixes"]
        level = plan.get("level", "1")
        existing = self._storage.get_user_mapping(user_id)
        umo_key = self._storage._umo_key(umo)

        if existing and existing != umo_key:
            old_data = sp.get(existing, {})
            if old_data:
                old_data = self._migrate_umo_data(old_data)
                new_data = sp.get(umo_key, {})
                if not new_data:
                    new_data = dict(old_data)
                self._storage.set_umo_data(umo, new_data)
                self._storage.remove_umo_by_key(existing)
                self._storage.unregister_umo(existing)

        umo_data = self._storage.get_umo_data(umo)
        order_time = datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M:%S") if create_time else "未知"

        # 基于订单购买时间计算实际剩余天数，而非绑定时间
        if create_time:
            base_time = datetime.fromtimestamp(create_time)
            elapsed_days = max(0, (datetime.now() - base_time).days)
            effective_days = max(0, days - elapsed_days)
        else:
            elapsed_days = 0
            effective_days = days

        if umo_data:
            umo_data = self._migrate_umo_data(umo_data)
            used_orders = umo_data.get("used_orders", [])
            if order_no and order_no in used_orders:
                self._wire(f"[AfdianModel] 订单{order_no}已在用户数据中，跳过绑定")
                return umo_data

            # 按等级累加天数（基于订单购买时间调整）
            if level == "2":
                umo_data["l2_days"] = umo_data.get("l2_days", 0) + effective_days
                umo_data["active_level"] = "2"  # 二级优先消耗
            else:
                umo_data["l1_days"] = umo_data.get("l1_days", 0) + effective_days
                if umo_data.get("active_level", "0") != "2":
                    umo_data["active_level"] = "1"

            umo_data["remaining_days"] = umo_data.get("l1_days", 0) + umo_data.get("l2_days", 0)

            existing_prefixes = self._storage._str_to_list(umo_data.get("prefixes", ""))
            combined_prefixes = list(set(existing_prefixes + prefixes))
            umo_data["prefixes"] = self._storage._list_to_str(combined_prefixes)
            umo_data["expire_time"] = (datetime.now() + timedelta(days=umo_data["remaining_days"])).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            umo_data["level"] = "2" if umo_data.get("l2_days", 0) > 0 else "1"
            if order_no:
                used_orders.append(order_no)
                umo_data["used_orders"] = used_orders
        else:
            # 新用户：基于订单购买时间计算实际剩余
            umo_data = {
                "remaining_days": effective_days,
                "prefixes": self._storage._list_to_str(prefixes),
                "order_time": order_time,
                "expire_time": (datetime.fromtimestamp(create_time) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S") if create_time else (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S"),
                "plan_id": plan_id,
                "used_orders": [order_no] if order_no else [],
                "level": level,
                "active_level": level,
                "l1_days": effective_days if level == "1" else 0,
                "l2_days": effective_days if level == "2" else 0,
            }
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
        """检查用户是否有某模型的使用权限。
        高级用户可使用低级模型：Lv2 > Lv1 > Lv0
        返回 (has_permission, available_prefixes)"""
        if not umo_data:
            return False, []

        # 先检查用户自己的前缀
        user_prefixes = umo_data.get("prefixes", [])
        if isinstance(user_prefixes, str):
            user_prefixes = self._storage._str_to_list(user_prefixes)

        for p in user_prefixes:
            if model_name.startswith(p) or p.startswith(model_name) or model_name == p:
                return True, user_prefixes

        user_level = umo_data.get("active_level", umo_data.get("level", "1"))

        # Lv2 用户可使用 Lv1 和 Lv0 模型
        if user_level == "2":
            level_1_prefixes = self._get_level_prefixes("1")
            for p in level_1_prefixes:
                if model_name.startswith(p) or p.startswith(model_name) or model_name == p:
                    combined = list(set(user_prefixes + level_1_prefixes))
                    return True, combined

        # Lv1 和 Lv2 用户可使用 Lv0 模型
        if user_level in ("1", "2"):
            level_0_prefixes = self._get_level_prefixes("0")
            for p in level_0_prefixes:
                if model_name.startswith(p) or p.startswith(model_name) or model_name == p:
                    combined = list(set(user_prefixes + level_0_prefixes))
                    return True, combined

        # Lv0 用户（含已降级）可使用公开模型
        if user_level == "0":
            level_0_prefixes = self._get_level_prefixes("0")
            for p in level_0_prefixes:
                if model_name.startswith(p) or p.startswith(model_name) or model_name == p:
                    combined = list(set(user_prefixes + level_0_prefixes))
                    return True, combined

        return False, user_prefixes

    def _get_level_prefixes(self, level: str) -> list:
        """获取指定等级的前缀列表"""
        try:
            config_fn = getattr(self._plan_manager, '_config_fn', None)
            if config_fn and callable(config_fn):
                cfg = config_fn()
                if level == "0":
                    raw = cfg.get("model_list", "")
                elif level == "1":
                    raw = cfg.get("models_1", "")
                elif level == "2":
                    raw = cfg.get("models_2", "")
                else:
                    return []
                prefixes = self._plan_manager._parse_models(raw)
                return prefixes
            else:
                self._wire("[AfdianModel] _get_level_prefixes: config_fn 不可用", "warning")
        except Exception as e:
            self._wire(f"[AfdianModel] _get_level_prefixes(Lv{level}) 失败: {e}", "error")
        return []
