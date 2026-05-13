from astrbot.core import sp
from .storage import StorageManager, SP_PLAN_MAPPING


class PlanManager:
    def __init__(self, config_fn, wire_fn=None):
        self._config_fn = config_fn
        self._wire = wire_fn or print
        self._storage = StorageManager(wire_fn)

    def sync_plan_mapping(self):
        existing = {}
        stored = sp.get(SP_PLAN_MAPPING, {})
        if stored:
            for plan_id, plan_data in stored.items():
                if not plan_id.startswith("_auto_"):
                    existing[plan_id] = plan_data

        updated = False
        for level in ("1", "2"):
            cfg = self._config_fn()
            plan_id = cfg.get(f"plan_id_{level}", "").strip()
            prefixes = self._parse_models(cfg.get(f"models_{level}", ""))
            if not plan_id or not prefixes:
                continue
            days = cfg.get(f"days_{level}", 30 if level == "1" else 365)
            existing[f"_auto_{plan_id}"] = {
                "days": days, 
                "prefixes": self._list_to_str(prefixes)
            }
            updated = True
            self._wire(f"[AfdianModel] 自动绑定 Lv{level}方案: {plan_id} -> {days}天 [{', '.join(prefixes)}]")
        if updated:
            self._storage.set_plan_mapping(existing)

    def get_plan_mapping(self) -> dict:
        mapping = sp.get(SP_PLAN_MAPPING, {})
        result = {}
        for plan_id, plan_data in mapping.items():
            clean_id = plan_id[6:] if plan_id.startswith("_auto_") else plan_id
            result[clean_id] = {
                "days": plan_data.get("days", 0),
                "prefixes": self._str_to_list(plan_data.get("prefixes", ""))
            }
        return result

    def verify_and_get_plan(self, order_plan_id: str) -> dict | None:
        self._wire(f"[AfdianModel] 验证plan_id: {order_plan_id}", "info")

        cfg = self._config_fn()
        plan_id_1 = cfg.get("plan_id_1", "").strip()
        plan_id_2 = cfg.get("plan_id_2", "").strip()

        self._wire(f"[AfdianModel] 配置中的plan_id_1: {plan_id_1}", "info")
        self._wire(f"[AfdianModel] 配置中的plan_id_2: {plan_id_2}", "info")

        if plan_id_1 and order_plan_id == plan_id_1:
            days = cfg.get("days_1", 30)
            prefixes = self._parse_models(cfg.get("models_1", ""))
            self._wire(f"[AfdianModel] 匹配到Lv1方案: days={days}, prefixes={prefixes}", "info")
            return {"days": days, "prefixes": prefixes, "level": "1"}

        if plan_id_2 and order_plan_id == plan_id_2:
            days = cfg.get("days_2", 365)
            prefixes = self._parse_models(cfg.get("models_2", ""))
            self._wire(f"[AfdianModel] 匹配到Lv2方案: days={days}, prefixes={prefixes}", "info")
            return {"days": days, "prefixes": prefixes, "level": "2"}

        self._wire(f"[AfdianModel] 未匹配到任何方案", "warning")
        return None

    @staticmethod
    def _parse_models(raw) -> list:
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str) and raw.strip():
            return [m.strip() for m in raw.split(",") if m.strip()]
        return []

    @staticmethod
    def _list_to_str(lst: list) -> str:
        """委托到 StorageManager 避免重复实现"""
        return StorageManager._list_to_str(lst)

    @staticmethod
    def _str_to_list(s: str) -> list:
        """委托到 StorageManager 避免重复实现"""
        return StorageManager._str_to_list(s)

    def match_prefixes(self, prefix: str, model_list: list) -> list:
        matched = []
        for m in model_list:
            if m == prefix:
                matched.append(m)
            elif m.startswith(prefix):
                matched.append(m)
            elif prefix.startswith(m) and len(m) > 0:
                matched.append(m)
        return matched
