"""定时任务 —— 每日权限到期检查 + 爱发电 API 轮询。"""

import asyncio
import json
import traceback
from datetime import datetime, timedelta

from .services import Services
from .storage import SP_UMO_PREFIX, StorageManager, sp_get, sp_get_global, sp_remove
from .utils import log_msg

DEFAULT_POLL_INTERVAL_HOURS = 1


class CronTasks:
    """定时任务管理器。"""

    def __init__(self, svc: Services) -> None:
        self._svc = svc

    def _poll_interval(self) -> int:
        """轮询间隔（秒），取自配置 poll_interval_hours，最小 1 小时。"""
        try:
            hours = int(
                self._svc.config_fn().get(
                    "poll_interval_hours", DEFAULT_POLL_INTERVAL_HOURS
                )
                or DEFAULT_POLL_INTERVAL_HOURS
            )
            return max(1, hours) * 3600
        except (TypeError, ValueError):
            return DEFAULT_POLL_INTERVAL_HOURS * 3600

    async def cron_daily(self) -> None:
        """每日零点：遍历活跃绑定，按等级递减天数，到期降级。"""
        while True:
            await asyncio.sleep(self._seconds_until_next_hour(0))
            try:
                await self._run_daily()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log_msg(self._svc.wire, f"每日零点定时任务异常: {e}", "error")
                log_msg(self._svc.wire, traceback.format_exc(), "error")

    async def cron_poll(self) -> None:
        """定时轮询爱发电 API，发现新订单自动标记。"""
        await asyncio.sleep(5)
        while True:
            api = self._svc.api_getter()
            if not api:
                log_msg(self._svc.wire, "Poll SKIP | API未配置", "warning")
                await asyncio.sleep(self._poll_interval())
                continue
            try:
                await self._run_poll(api)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log_msg(self._svc.wire, f"Poll FAIL | 异常: {e}", "error")
                log_msg(self._svc.wire, traceback.format_exc(), "error")
            await asyncio.sleep(self._poll_interval())

    # ── 内部 ──────────────────────────────────────

    async def _run_daily(self) -> None:
        active_umos = await self._svc.storage.get_active_umos()
        if not active_umos:
            log_msg(self._svc.wire, "Daily OK | 0 active")
            return

        total = len(active_umos)
        expired = 0
        switched = 0
        for key in list(active_umos):
            data = await self._svc.storage.get_umo_data_by_key(key)
            if not isinstance(data, dict):
                await self._svc.storage.remove_umo_by_key(key)
                await self._svc.storage.unregister_umo(key)
                expired += 1
                continue

            data = StorageManager.migrate_umo_data(data, self._svc.wire)
            active_level = data.get("active_level", "1")
            l1_days = data.get("l1_days", 0)
            l2_days = data.get("l2_days", 0)

            if active_level == "2":
                l2_days -= 1
                if l2_days <= 0:
                    if l1_days > 0:
                        data["active_level"] = "1"
                        data["level"] = "1"
                        switched += 1
                        l2_days = 0
                    else:
                        await self._downgrade_to_lv0(key)
                        expired += 1
                        continue
                data["l2_days"] = l2_days
            elif active_level == "1":
                l1_days -= 1
                if l1_days <= 0:
                    await self._downgrade_to_lv0(key)
                    expired += 1
                    continue
                data["l1_days"] = l1_days

            data["remaining_days"] = data.get("l1_days", 0) + data.get("l2_days", 0)
            data["expire_time"] = (
                datetime.now() + timedelta(days=data["remaining_days"])
            ).strftime("%Y-%m-%d %H:%M:%S")
            await self._svc.storage.set_umo_data_by_key(key, data)

        info = f" | Switched: {switched}" if switched > 0 else ""
        log_msg(self._svc.wire, f"Daily OK | {total} active | Expired: {expired}{info}")

    async def _downgrade_to_lv0(self, key: str) -> None:
        """降级用户到 Lv0：清空余量并把会话 provider 恢复为全局默认。"""
        data = await sp_get(key, {}) or {}
        if data:
            data = dict(data)
            data["active_level"] = "0"
            data["level"] = "0"
            data["l1_days"] = 0
            data["l2_days"] = 0
            data["remaining_days"] = 0
            await self._svc.storage.set_umo_data_by_key(key, data)
            await self._svc.storage.unregister_umo(key)

        current_key = key + ":current"
        current = await sp_get(current_key, "")
        default_provider = await sp_get_global("curr_provider", "")
        if current and default_provider and current != default_provider:
            try:
                umo = json.loads(key.replace(SP_UMO_PREFIX, ""))
                context = self._svc.astrbot_context
                if context is None:
                    log_msg(self._svc.wire, f"降级 {umo}: 无 Context，跳过 provider 恢复", "warning")
                else:
                    from astrbot.core.provider.entities import ProviderType
                    await context.provider_manager.set_provider(
                        default_provider, ProviderType.CHAT_COMPLETION, umo
                    )
                    log_msg(
                        self._svc.wire,
                        f"降级完成: {umo} 的 provider 已恢复为默认 ({default_provider})",
                    )
            except (json.JSONDecodeError, KeyError) as e:
                log_msg(self._svc.wire, f"降级解析 umo 失败: {e}", "warning")
            except Exception as e:
                log_msg(self._svc.wire, f"降级恢复 provider 失败: {e}", "error")
        await sp_remove(current_key)

    async def _run_poll(self, api) -> None:
        pg = 1
        total_scanned = 0
        while True:
            resp = await api.query_order(page=pg)
            if resp.get("ec") != 200:
                log_msg(self._svc.wire, f"Poll FAIL | p{pg}: {resp.get('em', resp)}", "warning")
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
        active = await self._svc.storage.get_active_umos()
        log_msg(self._svc.wire, f"Poll OK | {total_scanned} scanned ({pg}p) | {len(active)} active")

    async def _process_single_order(self, order: dict) -> None:
        """处理单笔已支付订单：已绑定用户自动累加天数；未绑定用户仅标记订单等待手动绑定。"""
        out_trade_no = order.get("out_trade_no", "")
        if not out_trade_no or order.get("status", 0) != 2:
            return

        plan_id = order.get("plan_id", "")
        plan_mapping = await self._svc.plan_manager.get_plan_mapping()
        if plan_id not in plan_mapping:
            return

        plan = plan_mapping[plan_id]
        days = plan["days"]
        prefixes = plan["prefixes"]
        user_id = order.get("user_id", "")
        umo_key = await self._svc.storage.get_user_mapping(user_id)

        umo_data: dict | None = None
        if umo_key:
            raw = await sp_get(umo_key, {}) or {}
            if raw:
                umo_data = StorageManager.migrate_umo_data(dict(raw), self._svc.wire)
                if out_trade_no in umo_data.get("used_orders", []):
                    if not await self._svc.storage.is_order_processed(out_trade_no):
                        await self._svc.storage.mark_order_processed(out_trade_no)
                    return

        if await self._svc.storage.is_order_processed(out_trade_no):
            return

        await self._svc.storage.mark_order_processed(out_trade_no)

        if not umo_key:
            log_msg(self._svc.wire, f"订单{out_trade_no} 用户{user_id} 未绑定，等待手动 /afdian_bind")
            return
        if not umo_data:
            log_msg(self._svc.wire, f"订单{out_trade_no} 用户{user_id} 映射数据为空，跳过自动发权", "warning")
            return

        plan_level = self._svc.plan_manager.infer_plan_level(prefixes)
        if plan_level == "2":
            umo_data["l2_days"] = umo_data.get("l2_days", 0) + days
            umo_data["active_level"] = "2"
        else:
            umo_data["l1_days"] = umo_data.get("l1_days", 0) + days
            if umo_data.get("active_level", "0") != "2":
                umo_data["active_level"] = "1"
        umo_data["remaining_days"] = umo_data.get("l1_days", 0) + umo_data.get("l2_days", 0)
        existing_pf = umo_data.get("prefixes", [])
        combined = list(set(existing_pf + prefixes))
        umo_data["prefixes"] = StorageManager._list_to_str(combined)
        umo_data["expire_time"] = (
            datetime.now() + timedelta(days=umo_data["remaining_days"])
        ).strftime("%Y-%m-%d %H:%M:%S")
        umo_data.setdefault("used_orders", []).append(out_trade_no)
        await self._svc.storage.set_umo_data_by_key(umo_key, umo_data)
        create_str = (
            datetime.fromtimestamp(order.get("create_time", 0)).strftime("%Y-%m-%d %H:%M:%S")
            if order.get("create_time")
            else "未知"
        )
        log_msg(
            self._svc.wire,
            f"用户{user_id}累加{days}天(Lv{plan_level}) 剩余{umo_data['remaining_days']}天 "
            f"订单{out_trade_no} @{create_str}",
        )

    @staticmethod
    def _seconds_until_next_hour(hour: int) -> int:
        now = datetime.now()
        target = datetime(now.year, now.month, now.day, hour, 0, 0)
        if now >= target:
            target += timedelta(days=1)
        return int((target - now).total_seconds())
