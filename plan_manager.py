"""赞助方案管理 —— plan_id 匹配、模型前缀解析、方案同步。"""

from .storage import SP_PLAN_MAPPING, sp_get
from .utils import list_to_str, log_msg, model_names_from_config, str_to_list


class PlanManager:
    """赞助方案管理器。"""

    def __init__(self, config_fn, storage: StorageManager, wire_fn=None) -> None:
        self._config_fn = config_fn
        self._storage = storage
        self._wire = wire_fn

    async def sync_plan_mapping(self) -> None:
        """将配置中的 Lv1/Lv2 方案同步到 sp plan_mapping（_auto_ 前缀标记自动方案）。"""
        existing: dict[str, dict] = {}
        stored = await sp_get(SP_PLAN_MAPPING, {}) or {}
        for plan_id, plan_data in stored.items():
            if not str(plan_id).startswith("_auto_"):
                existing[plan_id] = plan_data

        updated = False
        cfg = self._config_fn()
        for level in ("1", "2"):
            plan_id = str(cfg.get(f"plan_id_{level}", "") or "").strip()
            prefixes = model_names_from_config(cfg.get(f"models_{level}", ""))
            if not plan_id or not prefixes:
                continue
            days = cfg.get(f"days_{level}", 30 if level == "1" else 365)
            existing[f"_auto_{plan_id}"] = {"days": days, "prefixes": list_to_str(prefixes)}
            updated = True
            log_msg(self._wire, f"自动绑定 Lv{level}方案: {plan_id} -> {days}天 [{', '.join(prefixes)}]")

        if updated:
            await self._storage.set_plan_mapping(existing)

    async def get_plan_mapping(self) -> dict[str, dict]:
        """返回清洗后的 plan_mapping（去除 _auto_ 前缀）。"""
        mapping = await sp_get(SP_PLAN_MAPPING, {}) or {}
        result: dict[str, dict] = {}
        for plan_id, plan_data in mapping.items():
            clean_id = str(plan_id)[6:] if str(plan_id).startswith("_auto_") else str(plan_id)
            result[clean_id] = {
                "days": plan_data.get("days", 0),
                "prefixes": str_to_list(plan_data.get("prefixes", "")),
            }
        return result

    async def verify_and_get_plan(self, order_plan_id: str) -> dict | None:
        """统一方案源：先查 plan_mapping（含手动 addplan 与 _auto_），再回退配置。"""
        mapping = await self.get_plan_mapping()
        if order_plan_id in mapping:
            plan = mapping[order_plan_id]
            level = self.infer_plan_level(plan["prefixes"])
            return {"days": plan["days"], "prefixes": plan["prefixes"], "level": level}

        # 配置回退：映射尚未同步（如首次启动）时按配置匹配
        cfg = self._config_fn()
        for level in ("1", "2"):
            cfg_plan = str(cfg.get(f"plan_id_{level}", "") or "").strip()
            if cfg_plan and order_plan_id == cfg_plan:
                days = cfg.get(f"days_{level}", 30 if level == "1" else 365)
                prefixes = model_names_from_config(cfg.get(f"models_{level}", ""))
                log_msg(self._wire, f"配置回退匹配到 Lv{level}方案: plan_id={order_plan_id}")
                return {"days": days, "prefixes": prefixes, "level": level}

        log_msg(self._wire, f"未匹配到任何方案: plan_id={order_plan_id}", "warning")
        return None

    def infer_plan_level(self, prefixes: list[str]) -> str:
        """根据前缀是否命中 Lv2 模型推断方案等级（单向匹配）。"""
        cfg = self._config_fn()
        models_2 = model_names_from_config(cfg.get("models_2", ""))
        for p in prefixes:
            if p in models_2 or any(p.startswith(m) for m in models_2):
                return "2"
        return "1"

    @staticmethod
    def match_prefixes(prefix: str, model_list: list[str]) -> list[str]:
        """单向前缀匹配：模型名等于前缀或长于前缀即命中。"""
        matched: list[str] = []
        for m in model_list:
            if m == prefix or m.startswith(prefix):
                matched.append(m)
        return matched
