"""持久化存储 —— 完全基于 AstrBot 官方 sp 接口。

不再维护任何自定义 JSON 文件。所有数据通过 sp (SharedPreferences/SQLite) 持久化。
所有 read-modify-write 操作通过 asyncio.Lock 保护，防止并发覆盖。
"""

import asyncio
import copy
import datetime
from typing import Any, Callable

from astrbot.core import sp

from .utils import list_to_str, log_msg, str_to_list

# ── sp 键常量 ─────────────────────────────────────
SP_PLAN_MAPPING      = "afdian_model:plan_mapping"
SP_ACTIVE_UMOS       = "afdian_model:active_umos"
SP_UMO_PREFIX        = "afdian_model:umo:"
SP_BY_AFDIAN         = "afdian_model:by_afdian:"
SP_USER_INDEX        = "afdian_model:user_index"
SP_PROCESSED_ORDERS  = "afdian_model:processed_orders"
SP_DAILY_LOCK        = "afdian_model:daily_lock"   # 每日扣减去重锁


class StorageManager:
    """统一持久化管理器，完全基于 AstrBot sp。

    通过 asyncio.Lock 保护所有 read-modify-write 操作，
    确保并发场景下数据一致性。
    """

    _list_to_str = staticmethod(list_to_str)
    _str_to_list = staticmethod(str_to_list)

    def __init__(self, wire_fn=None) -> None:
        self._wire = wire_fn
        # asyncio.Lock 保护 read-modify-write 原子性
        # 在单线程 asyncio 事件循环中，同步的 sp.get/put 之间不会被打断，
        # 但跨多个 sp 操作的复合修改（如 register_umo 的读-改-写）需要锁保护，
        # 防止在 await 让出控制权后其他协程读到中间状态。
        self._lock = asyncio.Lock()

    # ── 订单去重 ──────────────────────────────────

    def is_order_processed(self, order_no: str) -> bool:
        return order_no in self._get_orders()

    async def mark_order_processed(self, order_no: str) -> None:
        """标记订单已处理（加锁保护 read-modify-write）。"""
        async with self._lock:
            self._mark_order_processed_nolock(order_no)

    def _mark_order_processed_nolock(self, order_no: str) -> None:
        """无锁版订单标记，仅在已持有锁时调用。"""
        orders = self._get_orders()
        orders.add(order_no)
        sp.put(SP_PROCESSED_ORDERS, list(orders))

    async def unmark_order_processed(self, order_no: str) -> None:
        """取消订单已处理标记（加锁保护 read-modify-write）。"""
        async with self._lock:
            orders = self._get_orders()
            orders.discard(order_no)
            sp.put(SP_PROCESSED_ORDERS, list(orders))

    def clear_orders(self) -> None:
        sp.put(SP_PROCESSED_ORDERS, [])

    def _get_orders(self) -> set[str]:
        raw = sp.get(SP_PROCESSED_ORDERS, [])
        return set(raw) if isinstance(raw, list) else set()

    # ── 每日扣减去重 ───────────────────────────────

    async def acquire_daily_lock(self) -> bool:
        """尝试获取每日扣减锁。返回 True 表示获取成功（今日尚未执行），False 表示已执行。

        使用日期字符串作为锁值，确保每天只执行一次扣减，
        防止插件重载导致 cron_daily 重复执行。

        必须在 _lock 保护下执行，防止两个协程同时读到"今日未执行"
        后都返回 True 的 TOCTOU 竞态。
        """
        async with self._lock:
            today = datetime.date.today().isoformat()
            current_lock = sp.get(SP_DAILY_LOCK, "")
            if current_lock == today:
                return False  # 今日已执行
            sp.put(SP_DAILY_LOCK, today)
            return True

    # ── sp 数据读写 ───────────────────────────────

    @staticmethod
    def _umo_key(umo) -> str:
        import json
        return f"{SP_UMO_PREFIX}{json.dumps(umo, separators=(',', ':'), sort_keys=True)}"

    def get_umo_data(self, umo) -> dict[str, Any]:
        data = sp.get(self._umo_key(umo), {}) or {}
        if data:
            data = self._validate_and_copy_umo_data(data)
            data["prefixes"] = str_to_list(data.get("prefixes", ""))
        return data

    def set_umo_data(self, umo, data: dict) -> None:
        sp.put(self._umo_key(umo), data)

    def get_umo_data_by_key(self, umo_key: str) -> dict[str, Any]:
        data = sp.get(umo_key, {}) or {}
        if data:
            data = self._validate_and_copy_umo_data(data)
            data["prefixes"] = str_to_list(data.get("prefixes", ""))
        return data

    def set_umo_data_by_key(self, umo_key: str, data: dict) -> None:
        sp.put(umo_key, data)

    async def update_umo_data_by_key_atomic(self, umo_key: str, modifier: Callable[[dict], dict]) -> dict:
        """原子性读取-修改-写入 umo_data。

        加锁确保在读取和写入之间不会有其他协程修改同一数据，
        防止并发覆盖导致数据丢失。

        Args:
            umo_key: sp 中的 umo 数据键
            modifier: 接收当前数据 dict（prefixes 为字符串），返回修改后的 dict

        Returns:
            修改后的数据 dict（prefixes 为列表，与 get_umo_data_by_key 一致）
        """
        async with self._lock:
            data = sp.get(umo_key, {}) or {}
            if data:
                data = self._validate_and_copy_umo_data(data)
            modified = modifier(data)
            sp.put(umo_key, modified)
            # 返回时转换 prefixes 为列表，与 get_umo_data_by_key 保持一致
            modified["prefixes"] = str_to_list(modified.get("prefixes", ""))
            return modified

    def remove_umo_by_key(self, umo_key: str) -> None:
        sp.put(umo_key, None)
        sp.put(umo_key + ":current", None)

    async def register_umo(self, umo_key: str) -> None:
        """注册活跃 umo（加锁保护 read-modify-write）。"""
        async with self._lock:
            active = sp.get(SP_ACTIVE_UMOS, [])
            if umo_key not in active:
                active.append(umo_key)
                sp.put(SP_ACTIVE_UMOS, active)

    async def unregister_umo(self, umo_key: str) -> None:
        """注销活跃 umo（加锁保护 read-modify-write）。"""
        async with self._lock:
            active = sp.get(SP_ACTIVE_UMOS, [])
            if umo_key in active:
                active.remove(umo_key)
                sp.put(SP_ACTIVE_UMOS, active)

    def get_active_umos(self) -> list:
        return sp.get(SP_ACTIVE_UMOS, [])

    def set_active_umos(self, active: list) -> None:
        sp.put(SP_ACTIVE_UMOS, active)

    def get_plan_mapping(self) -> dict:
        return sp.get(SP_PLAN_MAPPING, {})

    def set_plan_mapping(self, mapping: dict) -> None:
        sp.put(SP_PLAN_MAPPING, mapping)

    def get_user_mapping(self, user_id: str) -> str | None:
        return sp.get(f"{SP_BY_AFDIAN}{user_id}", None)

    async def set_user_mapping(self, user_id: str, umo_key: str) -> None:
        """设置用户映射（加锁保护 read-modify-write）。"""
        async with self._lock:
            sp.put(f"{SP_BY_AFDIAN}{user_id}", umo_key)
            self._register_user_id_nolock(user_id)

    async def remove_user_mapping(self, user_id: str) -> None:
        """移除用户映射（加锁保护 read-modify-write）。"""
        async with self._lock:
            sp.put(f"{SP_BY_AFDIAN}{user_id}", None)
            self._unregister_user_id_nolock(user_id)

    @staticmethod
    def _default_model() -> str:
        return sp.get("curr_provider", "", scope="global", scope_id="global") or "默认模型"

    def get_current_model(self, umo) -> str:
        return sp.get(self._umo_key(umo) + ":current", self._default_model())

    def set_current_model(self, umo, model: str) -> None:
        sp.put(self._umo_key(umo) + ":current", model)

    def set_current_model_by_key(self, umo_key: str, model: str) -> None:
        sp.put(umo_key + ":current", model)

    # ── 全量重置 ──────────────────────────────────

    async def full_reset(self) -> dict:
        """清除所有持久化数据。返回统计信息。"""
        async with self._lock:
            stats: dict[str, int] = {"orders": 0, "umo_data": 0, "user_mappings": 0, "active_umos": 0}

            orders = self._get_orders()
            stats["orders"] = len(orders)
            self.clear_orders()

            active = list(sp.get(SP_ACTIVE_UMOS, []))
            stats["active_umos"] = len(active)
            seen: set[str] = set()

            for umo_key in active:
                sp.put(umo_key, None)
                sp.put(umo_key + ":current", None)
                seen.add(umo_key)
                stats["umo_data"] += 1

            for user_id in sp.get(SP_USER_INDEX, []):
                umo_key = sp.get(f"{SP_BY_AFDIAN}{user_id}", None)
                if umo_key and umo_key not in seen:
                    sp.put(umo_key, None)
                    sp.put(umo_key + ":current", None)
                    stats["umo_data"] += 1
                sp.put(f"{SP_BY_AFDIAN}{user_id}", None)
                stats["user_mappings"] += 1

            sp.put(SP_ACTIVE_UMOS, [])
            sp.put(SP_PLAN_MAPPING, {})
            sp.put(SP_USER_INDEX, [])
            sp.put(SP_DAILY_LOCK, "")

            log_msg(self._wire, f"全量重置完成: {stats}")
            return stats

    # ── 旧数据迁移 ────────────────────────────────

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

    # ── 数据校验 ──────────────────────────────────

    @staticmethod
    def _validate_and_copy_umo_data(data: dict) -> dict:
        """深拷贝并校验 umo_data，修复常见数据不一致问题。

        防止因 sp 返回引用导致的浅拷贝问题，以及因崩溃/并发
        导致的字段缺失。确保返回的数据结构完整且独立。

        注意: prefixes 保持存储格式（字符串），由 get_umo_data /
        get_umo_data_by_key 在返回前转换为列表。
        """
        # 深拷贝：确保修改返回值不影响 sp 内部状态
        data = copy.deepcopy(data)

        # 确保关键字段存在且有正确类型
        if "prefixes" not in data:
            data["prefixes"] = ""
        elif isinstance(data["prefixes"], list):
            data["prefixes"] = list_to_str(data["prefixes"])

        # 确保 used_orders 存在（deepcopy 已保证独立性，无需再浅拷贝）
        if "used_orders" not in data:
            data["used_orders"] = []

        # 确保分级字段存在（兼容旧数据）
        if "l1_days" not in data:
            data["l1_days"] = 0
        if "l2_days" not in data:
            data["l2_days"] = 0
        if "active_level" not in data:
            data["active_level"] = data.get("level", "1")

        return data

    # ── 内部 ──────────────────────────────────────

    @staticmethod
    def _register_user_id_nolock(user_id: str) -> None:
        """无锁版用户 ID 注册，仅在已持有锁时调用。"""
        idx = sp.get(SP_USER_INDEX, [])
        if user_id not in idx:
            idx.append(user_id)
            sp.put(SP_USER_INDEX, idx)

    @staticmethod
    def _unregister_user_id_nolock(user_id: str) -> None:
        """无锁版用户 ID 注销，仅在已持有锁时调用。"""
        idx = sp.get(SP_USER_INDEX, [])
        if user_id in idx:
            idx.remove(user_id)
            sp.put(SP_USER_INDEX, idx)

    # 保留同步版本供非异步上下文（如 full_reset）使用
    def _register_user_id(self, user_id: str) -> None:
        idx = sp.get(SP_USER_INDEX, [])
        if user_id not in idx:
            idx.append(user_id)
            sp.put(SP_USER_INDEX, idx)

    def _unregister_user_id(self, user_id: str) -> None:
        idx = sp.get(SP_USER_INDEX, [])
        if user_id in idx:
            idx.remove(user_id)
            sp.put(SP_USER_INDEX, idx)
