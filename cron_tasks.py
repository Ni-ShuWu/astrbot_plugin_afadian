import asyncio
import os
import json
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

    def _migrate_data(self, data: dict) -> dict:
        """迁移旧数据到新的分级存储格式，委托给 StorageManager 统一实现"""
        return StorageManager.migrate_umo_data(data, self._wire)

    async def _cron_daily(self):
        while True:
            await asyncio.sleep(self._seconds_until_next_hour(0))
            try:
                active_umos = self._storage.get_active_umos()
                if not active_umos:
                    self._wire("[AfdianModel] Daily OK | Bindings: 0 active, nothing to do")
                    continue
                total = len(active_umos)
                expired = 0
                level_switched = 0
                for key in list(active_umos):
                    data = self._storage.get_umo_data_by_key(key)
                    if not data or not isinstance(data, dict):
                        self._storage.remove_umo_by_key(key)
                        self._storage.unregister_umo(key)
                        expired += 1
                        continue

                    data = self._migrate_data(data)
                    active_level = data.get("active_level", "1")
                    l1_days = data.get("l1_days", 0)
                    l2_days = data.get("l2_days", 0)

                    if active_level == "2":
                        # 消耗 Lv2 天数，Lv1 在此期间暂停
                        l2_days -= 1
                        if l2_days <= 0:
                            if l1_days > 0:
                                # Lv2 耗尽 → 切换至 Lv1，当天不消耗 Lv1
                                data["active_level"] = "1"
                                data["level"] = "1"
                                level_switched += 1
                                self._wire(f"[AfdianModel] UMO{key} Lv2耗尽，切换至Lv1(剩余{l1_days}天)")
                                l2_days = 0
                            else:
                                # 全部耗尽 → 降级为 Lv0
                                self._wire(f"[AfdianModel] UMO{key} 全部权限到期，降级为Lv0")
                                self._downgrade_to_lv0(key)
                                expired += 1
                                continue
                        data["l2_days"] = l2_days

                    elif active_level == "1":
                        l1_days -= 1
                        if l1_days <= 0:
                            self._wire(f"[AfdianModel] UMO{key} Lv1权限到期，降级为Lv0")
                            self._downgrade_to_lv0(key)
                            expired += 1
                            continue
                        data["l1_days"] = l1_days

                    # 更新总剩余天数
                    data["remaining_days"] = data.get("l1_days", 0) + data.get("l2_days", 0)
                    data["expire_time"] = (datetime.now() + timedelta(days=data["remaining_days"])).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    self._storage.set_umo_data_by_key(key, data)

                switched_info = f" | Switched: {level_switched}" if level_switched > 0 else ""
                self._wire(
                    f"[AfdianModel] Daily OK | Bindings: {total} active | "
                    f"Expired: {expired}{switched_info}"
                )
            except Exception as e:
                self._wire(f"[AfdianModel] 每日零点定时任务异常: {e}", "error")

    def _downgrade_to_lv0(self, key: str):
        """将过期用户降级为Lv0，保留绑定关系但清除付费权限"""
        data = sp.get(key, {})
        if data:
            data = dict(data)
            data["active_level"] = "0"
            data["level"] = "0"
            data["l1_days"] = 0
            data["l2_days"] = 0
            data["remaining_days"] = 0
            self._storage.set_umo_data_by_key(key, data)
        self._storage.unregister_umo(key)
        # 恢复默认 provider
        current_key = key + ":current"
        default_provider = sp.get("curr_provider", "")
        if sp.get(current_key) and default_provider:
            try:
                umo = json.loads(key.replace(SP_UMO_PREFIX, ""))
                try:
                    import asyncio as _asyncio
                    async def _restore():
                        from astrbot.core.provider.entities import ProviderType
                        context = sp.get("_context")
                        if context:
                            await context.provider_manager.set_provider(
                                default_provider, ProviderType.CHAT_COMPLETION, umo
                            )
                    _asyncio.create_task(_restore())
                except Exception:
                    sp.put(current_key, default_provider)
            except Exception:
                pass

    async def _cron_poll(self):
        await asyncio.sleep(5)
        while True:
            api = self._api_getter()
            if not api:
                self._wire("[AfdianModel] Poll SKIP | API未配置，等待下次尝试", "warning")
                await asyncio.sleep(POLL_INTERVAL)
                continue
            try:
                pg = 1
                total_scanned = 0
                while True:
                    resp = await api.query_order(page=pg)
                    if resp.get("ec") != 200:
                        self._wire(f"[AfdianModel] Poll FAIL | API错误 p{pg}: {resp}", "warning")
                        break
                    data = resp.get("data", {})
                    orders = data.get("list", [])
                    if not orders:
                        break
                    for order in orders:
                        await self._process_single_order(order)
                        total_scanned += 1
                    total_pages = data.get("total_page", 1)
                    if pg >= total_pages:
                        break
                    pg += 1
                active_umos = self._storage.get_active_umos()
                self._wire(
                    f"[AfdianModel] Poll OK | Orders: {total_scanned} scanned ({pg} pages) | "
                    f"Bindings: {len(active_umos)} active"
                )
            except Exception as e:
                self._wire(f"[AfdianModel] Poll FAIL | 异常: {e}", "error")
            await asyncio.sleep(POLL_INTERVAL)

    async def _process_single_order(self, order: dict):
        out_trade_no = order.get("out_trade_no", "")
        if not out_trade_no:
            return
        status = order.get("status", 0)
        if status != 2:
            return
        plan_id = order.get("plan_id", "")
        plan_mapping = self._plan_manager.get_plan_mapping()
        if plan_id not in plan_mapping:
            return
        plan = plan_mapping[plan_id]
        days = plan["days"]
        prefixes = plan["prefixes"]
        user_id = order.get("user_id", "")
        existing = self._storage.get_user_mapping(user_id)
        umo_key = existing if existing else None

        # ── 幂等去重：used_orders 优先（不依赖 processed_orders.json）──
        if umo_key:
            umo_data = sp.get(umo_key, {})
            if umo_data:
                umo_data = dict(umo_data)
                umo_data = self._migrate_data(umo_data)
                used_orders = umo_data.get("used_orders", [])
                if out_trade_no in used_orders:
                    # 同步标记到文件（兜底）
                    if not self._storage.is_order_processed(out_trade_no):
                        self._storage.mark_order_processed(out_trade_no)
                    return

        # 文件级去重（兜底：umodata 无此订单但文件有记录）
        if self._storage.is_order_processed(out_trade_no):
            return

        self._storage.mark_order_processed(out_trade_no)

        if umo_key:
            if not umo_data:
                umo_data = sp.get(umo_key, {})
                if not umo_data:
                    umo_data = {}
                else:
                    umo_data = dict(umo_data)
                    umo_data = self._migrate_data(umo_data)
            used_orders = umo_data.get("used_orders", [])
            plan_level = self._infer_plan_level(prefixes)
            if plan_level == "2":
                umo_data["l2_days"] = umo_data.get("l2_days", 0) + days
                umo_data["active_level"] = "2"
            else:
                umo_data["l1_days"] = umo_data.get("l1_days", 0) + days
                if umo_data.get("active_level", "0") != "2":
                    umo_data["active_level"] = "1"
            umo_data["remaining_days"] = umo_data.get("l1_days", 0) + umo_data.get("l2_days", 0)
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
                f"[AfdianModel] 用户{user_id}累加{days}天(Lv{plan_level})，剩余{umo_data['remaining_days']}天 "
                f"订单{out_trade_no} 下单时间"
                f"@{datetime.fromtimestamp(order.get('create_time',0)).strftime('%Y-%m-%d %H:%M:%S') if order.get('create_time') else '未知'}"
            )

    def _infer_plan_level(self, prefixes: list) -> str:
        """根据前缀推断方案等级。检查前缀是否属于 Lv2 > Lv1 > Lv0"""
        try:
            cfg_fn = getattr(self._plan_manager, '_config_fn', None)
            if cfg_fn and callable(cfg_fn):
                cfg = cfg_fn()
                models_2 = self._storage._str_to_list(cfg.get("models_2", ""))
                for p in prefixes:
                    if p in models_2 or any(p.startswith(m) or m.startswith(p) for m in models_2):
                        return "2"
                models_1 = self._storage._str_to_list(cfg.get("models_1", ""))
                for p in prefixes:
                    if p in models_1 or any(p.startswith(m) or m.startswith(p) for m in models_1):
                        return "1"
        except Exception:
            pass
        return "1"

    def _seconds_until_next_hour(self, hour: int) -> int:
        now = datetime.now()
        next_run = datetime(now.year, now.month, now.day, hour, 0, 0, 0)
        if now >= next_run:
            next_run += timedelta(days=1)
        return int((next_run - now).total_seconds())
