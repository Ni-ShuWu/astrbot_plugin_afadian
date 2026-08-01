"""持久化存储 —— 基于 AstrBot sp（SQLite）异步接口。

所有数据通过 sp.get_async / put_async / remove_async 读写（默认 scope=unknown），
与旧版同步 sp.get/put 的存储位置一致，无需数据迁移。
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
SP_PLUGIN_CONFIG     = "afdian_model:plugin_config"  # 旧配置副本，仅供一次性迁移读取


# ── 异步 sp 封装 ──────────────────────────────────

async def sp_get(key: str, default: Any = None) -> Any:
    return await sp.get_async("unknown", "unknown", key, default)


async def sp_put(key: str, value: Any) -> None:
    await sp.put_async("unknown", "unknown", key, value)


async def sp_remove(key: str) -> None:
    await sp.remove_async("unknown", "unknown", key)


async def sp_get_global(key: str, default: Any = None) -> Any:
    return await sp.get_async("global", "global", key, default)


class StorageManager:
    """统一持久化管理器（异步）。"""

    _list_to_str = staticmethod(list_to_str)
    _str_to_list = staticmethod(str_to_list)

    def __init__(self, wire_fn=None) -> None:
        self._wire = wire_fn

    # ── 订单去重 ──────────────────────────────────

    async def is_order_processed(self, order_no: str) -> bool:
        return order_no in await self._get_orders()

    async def mark_order_processed(self, order_no: str) -> None:
        orders = await self._get_orders()
        orders.add(order_no)
        await sp_put(SP_PROCESSED_ORDERS, list(orders))

    async def unmark_order_processed(self, order_no: str) -> None:
        orders = await self._get_orders()
        orders.discard(order_no)
        await sp_put(SP_PROCESSED_ORDERS, list(orders))

    async def clear_orders(self) -> None:
        await sp_put(SP_PROCESSED_ORDERS, [])

    async def _get_orders(self) -> set[str]:
        raw = await sp_get(SP_PROCESSED_ORDERS, [])
        return set(raw) if isinstance(raw, list) else set()

    # ── sp 数据读写 ───────────────────────────────

    @staticmethod
    def _umo_key(umo) -> str:
        import json
        return f"{SP_UMO_PREFIX}{json.dumps(umo, separators=(',', ':'), sort_keys=True)}"

    async def get_umo_data(self, umo) -> dict[str, Any]:
        data = await sp_get(self._umo_key(umo), {}) or {}
        if data:
            data = dict(data)
            data["prefixes"] = str_to_list(data.get("prefixes", ""))
        return data

    async def set_umo_data(self, umo, data: dict) -> None:
        await sp_put(self._umo_key(umo), data)

    async def get_umo_data_by_key(self, umo_key: str) -> dict[str, Any]:
        data = await sp_get(umo_key, {}) or {}
        if data:
            data = dict(data)
            data["prefixes"] = str_to_list(data.get("prefixes", ""))
        return data

    async def set_umo_data_by_key(self, umo_key: str, data: dict) -> None:
        await sp_put(umo_key, data)

    async def remove_umo_by_key(self, umo_key: str) -> None:
        await sp_remove(umo_key)
        await sp_remove(umo_key + ":current")

    async def register_umo(self, umo_key: str) -> None:
        active = await sp_get(SP_ACTIVE_UMOS, [])
        if umo_key not in active:
            active.append(umo_key)
            await sp_put(SP_ACTIVE_UMOS, active)

    async def unregister_umo(self, umo_key: str) -> None:
        active = await sp_get(SP_ACTIVE_UMOS, [])
        if umo_key in active:
            active.remove(umo_key)
            await sp_put(SP_ACTIVE_UMOS, active)

    async def get_active_umos(self) -> list:
        return await sp_get(SP_ACTIVE_UMOS, [])

    async def set_active_umos(self, active: list) -> None:
        await sp_put(SP_ACTIVE_UMOS, active)

    async def get_plan_mapping(self) -> dict:
        return await sp_get(SP_PLAN_MAPPING, {})

    async def set_plan_mapping(self, mapping: dict) -> None:
        await sp_put(SP_PLAN_MAPPING, mapping)

    async def get_user_mapping(self, user_id: str) -> str | None:
        return await sp_get(f"{SP_BY_AFDIAN}{user_id}", None)

    async def set_user_mapping(self, user_id: str, umo_key: str) -> None:
        await sp_put(f"{SP_BY_AFDIAN}{user_id}", umo_key)
        await self._register_user_id(user_id)

    async def remove_user_mapping(self, user_id: str) -> None:
        await sp_remove(f"{SP_BY_AFDIAN}{user_id}")
        await self._unregister_user_id(user_id)

    @staticmethod
    async def _default_model() -> str:
        return await sp_get_global("curr_provider", "") or "默认模型"

    async def get_current_model(self, umo) -> str:
        return await sp_get(self._umo_key(umo) + ":current", await self._default_model())

    async def set_current_model(self, umo, model: str) -> None:
        await sp_put(self._umo_key(umo) + ":current", model)

    async def set_current_model_by_key(self, umo_key: str, model: str) -> None:
        await sp_put(umo_key + ":current", model)

    # ── 全量重置 ──────────────────────────────────

    async def full_reset(self) -> dict:
        """清除所有持久化数据。返回统计信息。"""
        stats: dict[str, int] = {"orders": 0, "umo_data": 0, "user_mappings": 0, "active_umos": 0}

        orders = await self._get_orders()
        stats["orders"] = len(orders)
        await self.clear_orders()

        active = list(await sp_get(SP_ACTIVE_UMOS, []))
        stats["active_umos"] = len(active)
        seen: set[str] = set()

        for umo_key in active:
            await sp_remove(umo_key)
            await sp_remove(umo_key + ":current")
            seen.add(umo_key)
            stats["umo_data"] += 1

        for user_id in await sp_get(SP_USER_INDEX, []):
            umo_key = await sp_get(f"{SP_BY_AFDIAN}{user_id}", None)
            if umo_key and umo_key not in seen:
                await sp_remove(umo_key)
                await sp_remove(umo_key + ":current")
                stats["umo_data"] += 1
            await sp_remove(f"{SP_BY_AFDIAN}{user_id}")
            stats["user_mappings"] += 1

        await sp_put(SP_ACTIVE_UMOS, [])
        await sp_put(SP_PLAN_MAPPING, {})
        await sp_put(SP_USER_INDEX, [])

        log_msg(self._wire, f"全量重置完成: {stats}")
        return stats

    # ── 旧数据迁移（格式迁移，幂等） ───────────────

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

    async def _register_user_id(self, user_id: str) -> None:
        idx = await sp_get(SP_USER_INDEX, [])
        if user_id not in idx:
            idx.append(user_id)
            await sp_put(SP_USER_INDEX, idx)

    async def _unregister_user_id(self, user_id: str) -> None:
        idx = await sp_get(SP_USER_INDEX, [])
        if user_id in idx:
            idx.remove(user_id)
            await sp_put(SP_USER_INDEX, idx)
