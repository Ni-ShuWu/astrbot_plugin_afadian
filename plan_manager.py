"""赞助方案管理 —— plan_id 匹配、模型前缀解析、方案同步。"""

from .storage import SP_PLAN_MAPPING, StorageManager
from .utils import list_to_str, log_msg, model_names_from_config, str_to_list


class PlanManager:
    """赞助方案管理器。"""

    def __init__(self, config_fn, storage: StorageManager, wire_fn=None) -> None:
        self._config_fn = config_fn
        self._storage = storage
        self._wire = wire_fn

    def sync_plan_mapping(self) -> None:
        """将配置中的 Lv1/Lv2 方案同步到 sp plan_mapping。

        通过 storage 接口读写，确保与其他模块的锁机制一致。
        """
        existing: dict[str, dict] = {}
        stored = self._storage.get_plan_mapping() or {}
        for plan_id, plan_data in stored.items():
            # 保留非自动生成的方案（手动添加的），跳过旧的 _auto_ 条目（会重新生成）
            if not plan_id.startswith("_auto_"):
                existing[plan_id] = plan_data

        updated = False
        for level in ("1", "2"):
            cfg = self._config_fn()
            plan_id = cfg.get(f"plan_id_{level}", "").strip()
            prefixes = model_names_from_config(cfg.get(f"models_{level}", ""))
            if not plan_id or not prefixes:
                continue
            days = cfg.get(f"days_{level}", 30 if level == "1" else 365)
            existing[f"_auto_{plan_id}"] = {"days": days, "prefixes": list_to_str(prefixes)}
            updated = True
            log_msg(self._wire, f"自动绑定 Lv{level}方案: {plan_id} -> {days}天 [{', '.join(prefixes)}]")

        if updated:
            self._storage.set_plan_mapping(existing)

    def get_plan_mapping(self) -> dict[str, dict]:
        """返回清洗后的 plan_mapping（去除 _auto_ 前缀）。

        通过 storage 接口读取，确保与其他模块的锁机制一致。
        """
        mapping = self._storage.get_plan_mapping() or {}
        result: dict[str, dict] = {}
        for plan_id, plan_data in mapping.items():
            clean_id = plan_id[6:] if plan_id.startswith("_auto_") else plan_id
            result[clean_id] = {
                "days": plan_data.get("days", 0),
                "prefixes": str_to_list(plan_data.get("prefixes", "")),
            }
        return result

    def verify_and_get_plan(self, order_plan_id: str) -> dict | None:
        """验证订单的 plan_id 是否匹配配置中的方案，返回方案信息。"""
        cfg = self._config_fn()
        for level in ("1", "2"):
            cfg_plan = cfg.get(f"plan_id_{level}", "").strip()
            if cfg_plan and order_plan_id == cfg_plan:
                days = cfg.get(f"days_{level}", 30 if level == "1" else 365)
                prefixes = model_names_from_config(cfg.get(f"models_{level}", ""))
                log_msg(self._wire, f"匹配到 Lv{level}方案: days={days}, prefixes={prefixes}")
                return {"days": days, "prefixes": prefixes, "level": level}

        log_msg(self._wire, f"未匹配到任何方案: plan_id={order_plan_id}", "warning")
        return None

    @staticmethod
    def match_prefixes(prefix: str, model_list: list[str]) -> list[str]:
        """前缀匹配模型名列表。"""
        matched: list[str] = []
        for m in model_list:
            if m == prefix or m.startswith(prefix) or prefix.startswith(m):
                matched.append(m)
        return matched
