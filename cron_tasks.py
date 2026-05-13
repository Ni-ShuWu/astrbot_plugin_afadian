import asyncio
import os
from datetime import datetime, timedelta
from astrbot.core import sp
from .storage import StorageManager, SP_UMO_PREFIX
from .plan_manager import PlanManager


POLL_INTERVAL = 1 * 3600
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, "data")


class CronTasks:
    def __init__(
        self,
        api_getter,
        storage: StorageManager,
        plan_manager: PlanManager,
        wire_fn
    ):
        self._api_getter = api_getter
        self._storage = storage
        self._plan_manager = plan_manager
        self._wire = wire_fn

    async def _cron_daily(self):
        while True:
            await asyncio.sleep(self._seconds_until_next_hour(0))
            try:
                active_umos = self._storage.get_active_umos()
                total = len(active_umos)
                if not active_umos:
                    self._wire("[AfdianModel] Daily OK | Bindings: 0 active, nothing to do")
                    continue
                now = datetime.now()
                expired = 0
                for key in list(active_umos):
                    data = self._storage.get_umo_data_by_key(key)
                    if not data or not isinstance(data, dict):
                        self._storage.remove_umo_by_key(key)
                        self._storage.unregister_umo(key)
                        continue
                    days = data.get("remaining_days", 0)
                    if days <= 0:
                        self._storage.remove_umo_by_key(key)
                        self._storage.unregister_umo(key)
                        continue
                    days -= 1
                    if days <= 0:
                        self._wire(f"[AfdianModel] UMO{key}权限到期，清除绑定")
                        current_key = key + ":current"
                        default_provider = sp.get("curr_provider", "")
                        if sp.get(current_key) and default_provider:
                            try:
                                umo = __import__('json').loads(key.replace(SP_UMO_PREFIX, ""))
                                try:
                                    from astrbot.core.provider.entities import ProviderType
                                except ImportError:
                                    pass
                            except Exception:
                                pass
                        self._storage.remove_umo_by_key(key)
                        self._storage.unregister_umo(key)
                        expired += 1
                    else:
                        data["remaining_days"] = days
                        data["expire_time"] = (now + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                        self._storage.set_umo_data_by_key(key, data)
                remaining = len(self._storage.get_active_umos())
                self._wire(
                    f"[AfdianModel] Daily OK | Bindings: {total} -> {remaining} | "
                    f"Expired: {expired} cleaned"
                )
            except Exception as e:
                self._wire(f"[AfdianModel] 每日零点定时任务异常: {e}", "error")

    async def _cron_poll(self):
        await asyncio.sleep(5)
        while True:
            api = self._api_getter()
            if not api:
                self._wire("[AfdianModel] Poll SKIP | API未配置，等待下次尝试", "warning")
                await asyncio.sleep(POLL_INTERVAL)
                continue
            try:
                resp = await api.query_order(page=1)
                if resp.get("ec") != 200:
                    self._wire(f"[AfdianModel] Poll FAIL | API错误: {resp}", "warning")
                    await asyncio.sleep(POLL_INTERVAL)
                    continue
                orders = resp.get("data", {}).get("list", [])
                processed = 0
                for order in orders:
                    await self._process_single_order(order)
                    processed += 1
                active_umos = self._storage.get_active_umos()
                self._wire(
                    f"[AfdianModel] Poll OK | Orders: {processed} scanned | "
                    f"Bindings: {len(active_umos)} active"
                )
            except Exception as e:
                self._wire(f"[AfdianModel] Poll FAIL | 异常: {e}", "error")
            await asyncio.sleep(POLL_INTERVAL)

    async def _process_single_order(self, order: dict):
        out_trade_no = order.get("out_trade_no", "")
        if not out_trade_no:
            return
        if self._storage.is_order_processed(out_trade_no):
            return
        status = order.get("status", 0)
        if status != 2:
            return
        plan_id = order.get("plan_id", "")
        plan_mapping = self._plan_manager.get_plan_mapping()
        if plan_id not in plan_mapping:
            return
        self._storage.mark_order_processed(out_trade_no)
        plan = plan_mapping[plan_id]
        days = plan["days"]
        prefixes = plan["prefixes"]
        user_id = order.get("user_id", "")
        existing = self._storage.get_user_mapping(user_id)
        umo_key = existing if existing else None
        if umo_key:
            umo_data = sp.get(umo_key, {})
            if umo_data:
                used_orders = umo_data.get("used_orders", [])
                if out_trade_no in used_orders:
                    return
                umo_data["remaining_days"] += days
                existing_prefixes = self._storage._str_to_list(umo_data.get("prefixes", ""))
                combined_prefixes = list(set(existing_prefixes + prefixes))
                umo_data["prefixes"] = self._storage._list_to_str(combined_prefixes)
                umo_data["expire_time"] = (
                    datetime.now() + timedelta(days=umo_data["remaining_days"])
                ).strftime("%Y-%m-%d %H:%M:%S")
                used_orders.append(out_trade_no)
                umo_data["used_orders"] = used_orders
                self._storage.set_umo_data_by_key(umo_key, umo_data)
                self._wire(
                    f"[AfdianModel] 用户{user_id}累加{days}天，剩余{umo_data['remaining_days']}天 "
                    f"订单{order.get('out_trade_no','')} 下单时间"
                    f"@{datetime.fromtimestamp(order.get('create_time',0)).strftime('%Y-%m-%d %H:%M:%S') if order.get('create_time') else '未知'}"
                )

    def _seconds_until_next_hour(self, hour: int) -> int:
        now = datetime.now()
        next_run = datetime(now.year, now.month, now.day, hour, 0, 0, 0)
        if now >= next_run:
            next_run += timedelta(days=1)
        return int((next_run - now).total_seconds())
