"""定时任务 —— 每日权限到期检查 + 爱发电 API 轮询。

关键修复:
- cron_daily 添加每日去重锁，防止插件重载导致重复扣减
- _downgrade_to_lv0 接收已修改的数据，避免重新读取 sp 导致覆盖
- _process_single_order 使用原子操作和完整数据写入
- 所有 read-modify-write 操作通过 storage 的锁保护
"""

import asyncio
import json
from datetime import datetime, timedelta

from .services import Services
from .storage import SP_UMO_PREFIX, StorageManager
from .utils import log_msg, str_to_list


POLL_INTERVAL = 3600  # 1 小时


class CronTasks:
    """定时任务管理器。"""

    def __init__(self, svc: Services) -> None:
        self._svc = svc

    async def cron_daily(self) -> None:
        """每日零点：遍历活跃绑定，按等级递减天数，到期降级。"""
        try:
            while True:
                await asyncio.sleep(self._seconds_until_next_hour(0))
                try:
                    await self._run_daily()
                except Exception as e:
                    log_msg(self._svc.wire, f"每日零点定时任务异常: {e}", "error")
        except asyncio.CancelledError:
            pass  # 插件重载时静默退出

    async def cron_poll(self) -> None:
        """定时轮询爱发电 API，发现新订单自动标记。"""
        await asyncio.sleep(5)
        try:
            while True:
                api = self._svc.api_getter()
                if not api:
                    log_msg(self._svc.wire, "Poll SKIP | API未配置", "warning")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                try:
                    await self._run_poll(api)
                except Exception as e:
                    log_msg(self._svc.wire, f"Poll FAIL | 异常: {e}", "error")
                await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            pass  # 插件重载时静默退出

    # ── 内部 ──────────────────────────────────────

    async def _run_daily(self) -> None:
        # 每日去重：如果今日已执行过扣减，跳过
        # 防止插件重载导致 cron_daily 重复执行
        if not self._svc.storage.acquire_daily_lock():
            log_msg(self._svc.wire, "Daily SKIP | 今日已执行过扣减")
            return

        active_umos = self._svc.storage.get_active_umos()
        if not active_umos:
            log_msg(self._svc.wire, "Daily OK | 0 active")
            return

        total = len(active_umos)
        expired = 0
        switched = 0
        for key in list(active_umos):
            # 使用原子操作读取-修改-写入，防止并发覆盖
            async with self._svc.storage._lock:
                data = self._svc.storage.get_umo_data_by_key(key)
                if not isinstance(data, dict) or not data:
                    self._svc.storage.remove_umo_by_key(key)
                    # 已持有锁，直接操作 sp 避免死锁
                    active = self._svc.sp.get("afdian_model:active_umos", [])
                    if key in active:
                        active.remove(key)
                        self._svc.sp.put("afdian_model:active_umos", active)
                    expired += 1
                    continue

                data = StorageManager.migrate_umo_data(data, self._svc.wire)
                active_level = data.get("active_level", "1")
                l1_days = data.get("l1_days", 0)
                l2_days = data.get("l2_days", 0)

                should_downgrade = False

                if active_level == "2":
                    l2_days -= 1
                    if l2_days <= 0:
                        if l1_days > 0:
                            data["active_level"] = "1"
                            data["level"] = "1"
                            switched += 1
                            l2_days = 0
                        else:
                            should_downgrade = True
                    data["l2_days"] = l2_days
                elif active_level == "1":
                    l1_days -= 1
                    if l1_days <= 0:
                        should_downgrade = True
                    data["l1_days"] = l1_days

                if should_downgrade:
                    # 直接使用已读取的 data 进行降级，不再重新从 sp 读取
                    self._apply_downgrade_to_lv0(key, data)
                    expired += 1
                    continue

                data["remaining_days"] = data.get("l1_days", 0) + data.get("l2_days", 0)
                data["expire_time"] = (datetime.now() + timedelta(days=data["remaining_days"])).strftime("%Y-%m-%d %H:%M:%S")
                self._svc.storage.set_umo_data_by_key(key, data)

        info = f" | Switched: {switched}" if switched > 0 else ""
        log_msg(self._svc.wire, f"Daily OK | {total} active | Expired: {expired}{info}")

    def _apply_downgrade_to_lv0(self, key: str, data: dict) -> None:
        """降级用户到 Lv0，保留绑定关系，恢复默认 provider。

        直接使用已读取的 data 进行修改，避免重新从 sp 读取导致覆盖并发写入。
        注意: 调用方必须已持有 storage._lock。
        """
        data["active_level"] = "0"
        data["level"] = "0"
        data["l1_days"] = 0
        data["l2_days"] = 0
        data["remaining_days"] = 0
        self._svc.storage.set_umo_data_by_key(key, data)
        # unregister_umo 需要锁，但调用方已持有锁，使用同步方式操作
        active = self._svc.sp.get("afdian_model:active_umos", [])
        if key in active:
            active.remove(key)
            self._svc.sp.put("afdian_model:active_umos", active)

        current_key = key + ":current"
        default_provider = self._svc.sp.get("curr_provider", "", scope="global", scope_id="global")
        if self._svc.sp.get(current_key) and default_provider:
            try:
                umo = json.loads(key.replace(SP_UMO_PREFIX, ""))
                from astrbot.core.provider.entities import ProviderType
                context = self._svc.sp.get("_context")
                if context:
                    async def _restore():
                        await context.provider_manager.set_provider(default_provider, ProviderType.CHAT_COMPLETION, umo)
                    asyncio.create_task(_restore())
            except (json.JSONDecodeError, KeyError):
                self._svc.sp.put(current_key, default_provider)

    async def _run_poll(self, api) -> None:
        pg = 1
        total_scanned = 0
        while True:
            resp = await api.query_order(page=pg)
            if resp.get("ec") != 200:
                log_msg(self._svc.wire, f"Poll FAIL | p{pg}: {resp}", "warning")
                break
            data = resp.get("data", {})
            orders = data.get("list", [])
            if not orders:
                break
            for order in orders:
                await self._process_single_order(order)
                total_scanned += 1
            if pg >= data.get("total_page", 1):
                break
            pg += 1
        active = self._svc.storage.get_active_umos()
        log_msg(self._svc.wire, f"Poll OK | {total_scanned} scanned ({pg}p) | {len(active)} active")

    async def _process_single_order(self, order: dict) -> None:
        out_trade_no = order.get("out_trade_no", "")
        if not out_trade_no or order.get("status", 0) != 2:
            return

        plan_id = order.get("plan_id", "")
        plan_mapping = self._svc.plan_manager.get_plan_mapping()
        if plan_id not in plan_mapping:
            return

        plan = plan_mapping[plan_id]
        days = plan["days"]
        prefixes = plan["prefixes"]
        user_id = order.get("user_id", "")
        umo_key = self._svc.storage.get_user_mapping(user_id)

        # 使用原子操作处理订单，防止并发覆盖
        async with self._svc.storage._lock:
            if umo_key:
                umo_data = self._svc.storage.get_umo_data_by_key(umo_key)
                if umo_data:
                    umo_data = StorageManager.migrate_umo_data(umo_data, self._svc.wire)
                    if out_trade_no in umo_data.get("used_orders", []):
                        # 订单已处理，确保全局标记也存在
                        if not self._svc.storage.is_order_processed(out_trade_no):
                            self._svc.storage._mark_order_processed_nolock(out_trade_no)
                        return

            if self._svc.storage.is_order_processed(out_trade_no):
                return

            # 使用无锁版标记，因为已持有锁
            self._svc.storage._mark_order_processed_nolock(out_trade_no)

            if umo_key:
                # 重新读取最新数据（在锁内，确保一致性）
                umo_data = self._svc.storage.get_umo_data_by_key(umo_key)
                if not umo_data:
                    umo_data = {}
                umo_data = StorageManager.migrate_umo_data(umo_data, self._svc.wire)

                plan_level = self._infer_plan_level(prefixes)
                if plan_level == "2":
                    umo_data["l2_days"] = umo_data.get("l2_days", 0) + days
                    umo_data["active_level"] = "2"
                else:
                    umo_data["l1_days"] = umo_data.get("l1_days", 0) + days
                    if umo_data.get("active_level", "0") != "2":
                        umo_data["active_level"] = "1"
                umo_data["remaining_days"] = umo_data.get("l1_days", 0) + umo_data.get("l2_days", 0)
                existing_pf = str_to_list(umo_data.get("prefixes", ""))
                combined = list(set(existing_pf + prefixes))
                umo_data["prefixes"] = StorageManager._list_to_str(combined)
                umo_data["expire_time"] = (datetime.now() + timedelta(days=umo_data["remaining_days"])).strftime("%Y-%m-%d %H:%M:%S")
                umo_data.setdefault("used_orders", [])
                if out_trade_no not in umo_data["used_orders"]:
                    umo_data["used_orders"].append(out_trade_no)
                # 确保关键字段完整
                umo_data.setdefault("level", plan_level)
                umo_data.setdefault("order_time",
                    datetime.fromtimestamp(order.get("create_time", 0)).strftime("%Y-%m-%d %H:%M:%S")
                    if order.get("create_time") else "未知"
                )
                self._svc.storage.set_umo_data_by_key(umo_key, umo_data)

                # 如果用户之前被降级到 Lv0（不在 active_umos 中），重新注册
                active_umos = self._svc.storage.get_active_umos()
                if umo_key not in active_umos:
                    active_umos.append(umo_key)
                    self._svc.sp.put("afdian_model:active_umos", active_umos)

                create_str = datetime.fromtimestamp(order.get("create_time", 0)).strftime("%Y-%m-%d %H:%M:%S") if order.get("create_time") else "未知"
                log_msg(self._svc.wire, f"用户{user_id}累加{days}天(Lv{plan_level}) 剩余{umo_data['remaining_days']}天 订单{out_trade_no} @{create_str}")

    def _infer_plan_level(self, prefixes: list[str]) -> str:
        cfg_fn = getattr(self._svc.plan_manager, "_config_fn", None)
        if not callable(cfg_fn):
            return "1"
        cfg = cfg_fn()
        models_2 = str_to_list(cfg.get("models_2", ""))
        for p in prefixes:
            if p in models_2 or any(p.startswith(m) or m.startswith(p) for m in models_2):
                return "2"
        return "1"

    @staticmethod
    def _seconds_until_next_hour(hour: int) -> int:
        now = datetime.now()
        target = datetime(now.year, now.month, now.day, hour, 0, 0)
        if now >= target:
            target += timedelta(days=1)
        return int((target - now).total_seconds())
