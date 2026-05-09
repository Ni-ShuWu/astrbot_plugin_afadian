import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timedelta

import aiohttp
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core import sp
from astrbot.core.provider.entities import ProviderType

API_BASE = "https://afdian.net/api/open"
POLL_INTERVAL = 6 * 3600
SP_PLAN_MAPPING = "afdian_model:plan_mapping"
SP_GROUP_ADMINS = "afdian_model:group_admins"
SP_ACTIVE_UMOS = "afdian_model:active_umos"
SP_UMO_PREFIX = "afdian_model:umo:"
ORDERS_FILE = "processed_orders.json"

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
ORDERS_PATH = os.path.join(PLUGIN_DIR, ORDERS_FILE)


class AfdianAPI:
    def __init__(self, user_id: str, token: str):
        self.user_id = user_id
        self.token = token

    def _sign(self, params: dict) -> tuple:
        json_str = json.dumps(params, separators=(",", ":"))
        ts = int(time.time())
        raw = f"{self.token}params{json_str}ts{ts}user_id{self.user_id}"
        sign = hashlib.md5(raw.encode()).hexdigest()
        return sign, ts

    async def _post(self, endpoint: str, params: dict) -> dict:
        sign, ts = self._sign(params)
        body = {
            "user_id": self.user_id,
            "params": json.dumps(params),
            "ts": ts,
            "sign": sign,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{API_BASE}/{endpoint}", json=body, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    return await resp.json()
        except Exception as e:
            logger.error(f"[AfdianModel] API请求失败: {endpoint} - {e}")
            return {"ec": -1, "em": str(e)}

    async def query_order(self, page: int = 1) -> dict:
        return await self._post("query-order", {"page": page})

    async def ping(self) -> dict:
        return await self._post("ping", {})


class AfdianModelPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._api = None
        self._processed_orders = self._load_processed_orders()

        asyncio.create_task(self._cron_daily())
        asyncio.create_task(self._cron_poll())

        context.register_web_api(
            "/api/v1/afdian/webhook", self._handle_webhook, ["POST"], "爱发电Webhook回调"
        )

    def _get_api(self):
        if self._api is None:
            cfg = self._config()
            uid = cfg.get("afdian_user_id", "")
            token = cfg.get("afdian_token", "")
            if uid and token:
                self._api = AfdianAPI(uid, token)
        return self._api

    def _config(self) -> dict:
        try:
            return self.context.get_star_config() or {}
        except Exception:
            return {}

    def _load_processed_orders(self) -> set:
        try:
            if os.path.exists(ORDERS_PATH):
                with open(ORDERS_PATH, "r", encoding="utf-8") as f:
                    return set(json.load(f))
        except Exception:
            pass
        return set()

    def _save_processed_orders(self):
        try:
            with open(ORDERS_PATH, "w", encoding="utf-8") as f:
                json.dump(list(self._processed_orders), f)
        except Exception as e:
            logger.error(f"[AfdianModel] 保存订单记录失败: {e}")

    def _get_plan_mapping(self) -> dict:
        return sp.get(SP_PLAN_MAPPING, {})

    def _get_group_admins(self) -> dict:
        return sp.get(SP_GROUP_ADMINS, {})

    def _umo_key(self, umo) -> str:
        return f"{SP_UMO_PREFIX}{json.dumps(umo, separators=(',', ':'), sort_keys=True)}"

    def _get_umo_data(self, umo) -> dict:
        return sp.get(self._umo_key(umo), {})

    def _set_umo_data(self, umo, data: dict):
        sp.put(self._umo_key(umo), data)

    def _verify_rsa(self, raw_body: bytes, signature: str, public_key_pem: str) -> bool:
        try:
            key = RSA.import_key(public_key_pem)
            h = SHA256.new(raw_body)
            sig_bytes = bytes.fromhex(signature)
            pkcs1_15.new(key).verify(h, sig_bytes)
            return True
        except Exception as e:
            logger.warning(f"[AfdianModel] RSA验签失败: {e}")
            return False

    async def _check_group_admin(self, event: AstrMessageEvent) -> bool:
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        if not group_id:
            return False
        try:
            adapter = event.get_platform_adapter()
            if adapter and hasattr(adapter, "get_group_member_info"):
                info = await adapter.get_group_member_info(group_id, sender_id)
                if info and info.get("role") in ("owner", "admin"):
                    return True
        except Exception as e:
            logger.debug(f"[AfdianModel] 平台API获取群角色失败，使用静态列表兜底: {e}")
        group_admins = self._get_group_admins()
        allowed = group_admins.get(str(group_id), [])
        return str(sender_id) in allowed

    def _match_prefixes(self, prefix: str, model_list: list) -> list:
        return [m for m in model_list if m.startswith(prefix)]

    async def _handle_webhook(self, request):
        cfg = self._config()
        public_key = cfg.get("afdian_public_key", "").strip()
        try:
            raw_body = await request.get_data()
            body = json.loads(raw_body)
        except Exception:
            return {"ec": 400, "em": "Invalid JSON"}
        if public_key:
            signature = request.headers.get("X-Af-Signature", "") or request.headers.get("x-af-signature", "")
            if signature:
                if not self._verify_rsa(raw_body, signature, public_key):
                    logger.warning("[AfdianModel] Webhook RSA验签失败")
                    return {"ec": 401, "em": "Signature verification failed"}
            else:
                logger.debug("[AfdianModel] Webhook未携带签名头，跳过RSA验证")
        data = body.get("data", {})
        if data.get("type") != "order":
            return {"ec": 200, "em": ""}
        order = data.get("order", {})
        await self._process_single_order(order)
        return {"ec": 200, "em": ""}

    def _register_umo(self, umo_key: str):
        active = sp.get(SP_ACTIVE_UMOS, [])
        if umo_key not in active:
            active.append(umo_key)
            sp.put(SP_ACTIVE_UMOS, active)

    def _unregister_umo(self, umo_key: str):
        active = sp.get(SP_ACTIVE_UMOS, [])
        if umo_key in active:
            active.remove(umo_key)
            sp.put(SP_ACTIVE_UMOS, active)

    async def _process_single_order(self, order: dict):
        out_trade_no = order.get("out_trade_no", "")
        if not out_trade_no:
            return
        if out_trade_no in self._processed_orders:
            return
        self._processed_orders.add(out_trade_no)
        self._save_processed_orders()

        status = order.get("status", 0)
        if status != 2:
            logger.info(f"[AfdianModel] 订单{out_trade_no}未支付，状态: {status}")
            return

        plan_id = order.get("plan_id", "")
        plan_mapping = self._get_plan_mapping()
        if plan_id not in plan_mapping:
            logger.info(f"[AfdianModel] 方案{plan_id}未配置，订单{out_trade_no}")
            return

        plan = plan_mapping[plan_id]
        days = plan["days"]
        prefixes = plan["prefixes"]
        user_id = order.get("user_id", "")

        existing = sp.get(f"{SP_UMO_PREFIX}by_afdian:{user_id}", None)
        umo_key = existing if existing else None
        if umo_key:
            umo_data = sp.get(umo_key, {})
            if umo_data:
                umo_data["remaining_days"] += days
                umo_data["prefixes"] = list(set(umo_data.get("prefixes", []) + prefixes))
                umo_data["expire_time"] = (
                    datetime.now() + timedelta(days=umo_data["remaining_days"])
                ).strftime("%Y-%m-%d %H:%M:%S")
                sp.put(umo_key, umo_data)
                logger.info(
                    f"[AfdianModel] 用户{user_id}累加{days}天，剩余{umo_data['remaining_days']}天"
                )

    async def _bind_user(self, user_id: str, plan_id: str, plan: dict, umo):
        days = plan["days"]
        prefixes = plan["prefixes"]
        existing = sp.get(f"{SP_UMO_PREFIX}by_afdian:{user_id}", None)
        umo_key = self._umo_key(umo)
        if existing and existing != umo_key:
            old_data = sp.get(existing, {})
            if old_data:
                new_data = sp.get(umo_key, {})
                if not new_data:
                    new_data = {"remaining_days": old_data.get("remaining_days", 0),
                                "prefixes": old_data.get("prefixes", []),
                                "expire_time": old_data.get("expire_time", ""),
                                "plan_id": old_data.get("plan_id", "")}
                    self._set_umo_data(umo, new_data)
                sp.put(existing, None)
                self._unregister_umo(existing)
        umo_data = self._get_umo_data(umo)
        if umo_data:
            umo_data["remaining_days"] += days
            umo_data["prefixes"] = list(set(umo_data.get("prefixes", []) + prefixes))
        else:
            umo_data = {"remaining_days": days, "prefixes": prefixes, "plan_id": plan_id}
        umo_data["expire_time"] = (datetime.now() + timedelta(days=umo_data["remaining_days"])).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self._set_umo_data(umo, umo_data)
        self._register_umo(umo_key)
        sp.put(f"{SP_UMO_PREFIX}by_afdian:{user_id}", umo_key)
        return umo_data

    # ==================== 用户命令 ====================

    @filter.command("afdian_bind")
    async def cmd_bind(self, event: AstrMessageEvent):
        """绑定爱发电订单号，获得LLM模型选择权限"""
        if event.get_group_id():
            yield event.plain_result("请在私聊中使用此命令")
            return
        api = self._get_api()
        if not api:
            yield event.plain_result("插件未配置爱发电API，请联系管理员")
            return
        parts = event.message_str.strip().split()
        if len(parts) < 3:
            yield event.plain_result("用法: /afdian_bind <订单号>")
            return
        order_no = parts[2]
        resp = await api.query_order(page=1)
        if resp.get("ec") != 200:
            yield event.plain_result("查询订单失败，请稍后重试")
            return
        order_list = resp.get("data", {}).get("list", [])
        order = None
        for o in order_list:
            if o.get("out_trade_no") == order_no:
                order = o
                break

        total_pages = resp.get("data", {}).get("total_page", 1)
        pg = 2
        while order is None and pg <= total_pages:
            resp = await api.query_order(page=pg)
            if resp.get("ec") != 200:
                break
            for o in resp.get("data", {}).get("list", []):
                if o.get("out_trade_no") == order_no:
                    order = o
                    break
            pg += 1

        if order is None:
            yield event.plain_result("未找到该订单，请检查订单号是否正确")
            return
        if order.get("status") != 2:
            yield event.plain_result("该订单未支付，无法绑定")
            return
        if order_no in self._processed_orders:
            yield event.plain_result("该订单已被使用")
            return

        plan_id = order.get("plan_id", "")
        plan_mapping = self._get_plan_mapping()
        if plan_id not in plan_mapping:
            yield event.plain_result("方案未配置，请联系管理员")
            return

        self._processed_orders.add(order_no)
        self._save_processed_orders()

        umo = event.unified_msg_origin
        plan = plan_mapping[plan_id]
        umo_data = await self._bind_user(order.get("user_id", ""), plan_id, plan, umo)

        model_list = self._config().get("model_list", [])
        available = []
        for p in umo_data["prefixes"]:
            available.extend(self._match_prefixes(p, model_list))

        yield event.plain_result(
            f"绑定成功，方案：{plan_id}（{plan['days']}天），"
            f"剩余{umo_data['remaining_days']}天，"
            f"可用模型：{', '.join(available) if available else '无'}"
        )

    @filter.command("afdian_models")
    async def cmd_models(self, event: AstrMessageEvent):
        """查看当前可用的LLM模型列表"""
        if event.get_group_id():
            yield event.plain_result("请在私聊中使用此命令")
            return
        umo_data = self._get_umo_data(event.unified_msg_origin)
        if not umo_data:
            yield event.plain_result("你还没有赞助权限，请先通过爱发电赞助后使用 /afdian_bind <订单号> 绑定")
            return
        model_list = self._config().get("model_list", [])
        available = []
        for p in umo_data.get("prefixes", []):
            available.extend(self._match_prefixes(p, model_list))
        current = sp.get(self._umo_key(event.unified_msg_origin) + ":current", "默认模型")
        yield event.plain_result(
            f"可用模型：{', '.join(available) if available else '无'}\n当前模型：{current}"
        )

    @filter.command("afdian_switch")
    async def cmd_switch(self, event: AstrMessageEvent):
        """切换当前使用的LLM模型，私聊切换个人模型，群聊切换全群模型（需群管权限）"""
        parts = event.message_str.strip().split()
        if len(parts) < 4:
            yield event.plain_result("用法: /afdian_switch <前缀> <模型名>")
            return
        prefix = parts[2]
        model_name = parts[3]
        group_id = event.get_group_id()

        if group_id:
            is_admin = await self._check_group_admin(event)
            if not is_admin:
                yield event.plain_result("仅群主或群管可切换群模型")
                return
            umo = event.unified_msg_origin
        else:
            umo = event.unified_msg_origin
            umo_data = self._get_umo_data(umo)
            if not umo_data:
                yield event.plain_result("无赞助权限，请先绑定")
                return
            prefixes = umo_data.get("prefixes", [])
            if prefix not in prefixes:
                yield event.plain_result("无此模型前缀的使用权限")
                return

        model_list = self._config().get("model_list", [])
        matched = self._match_prefixes(prefix, model_list)
        if model_name not in matched:
            yield event.plain_result(f"模型{model_name}不在可用列表中，可用: {', '.join(matched) if matched else '无'}")
            return

        try:
            self.context.provider_manager.set_provider(
                model_name, ProviderType.CHAT_COMPLETION, umo
            )
        except Exception as e:
            logger.warning(f"[AfdianModel] set_provider失败，尝试备用方式: {e}")
            try:
                sp_key = f"curr_provider_{json.dumps(umo, separators=(',', ':'), sort_keys=True)}"
                sp.put(sp_key, model_name)
            except Exception as e2:
                yield event.plain_result(f"切换模型失败: {e2}")
                return

        sp.put(self._umo_key(umo) + ":current", model_name)
        yield event.plain_result(f"已切换至{model_name}")

    @filter.command("afdian_status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看赞助权限状态：剩余天数、当前模型、到期时间"""
        if event.get_group_id():
            yield event.plain_result("请在私聊中使用此命令")
            return
        umo_data = self._get_umo_data(event.unified_msg_origin)
        if not umo_data:
            yield event.plain_result("无赞助权限，请先绑定")
            return
        current = sp.get(self._umo_key(event.unified_msg_origin) + ":current", "默认模型")
        yield event.plain_result(
            f"剩余天数：{umo_data['remaining_days']}\n"
            f"当前模型：{current}\n"
            f"到期时间：{umo_data.get('expire_time', '未知')}"
        )

    # ==================== 管理员命令 ====================

    @filter.command("afdian_addplan")
    async def cmd_addplan(self, event: AstrMessageEvent):
        """添加赞助方案映射：plan_id -> 天数 + 模型前缀"""
        if not await self._check_admin(event):
            yield event.plain_result("无权限")
            return
        parts = event.message_str.strip().split()
        if len(parts) < 5:
            yield event.plain_result("用法: /afdian_addplan <plan_id> <天数> <前缀1,前缀2,...>")
            return
        plan_id = parts[2]
        try:
            days = int(parts[3])
        except ValueError:
            yield event.plain_result("天数必须为整数")
            return
        prefixes = [p.strip() for p in parts[4].split(",") if p.strip()]
        if not prefixes:
            yield event.plain_result("至少需要一个模型前缀")
            return
        mapping = self._get_plan_mapping()
        mapping[plan_id] = {"days": days, "prefixes": prefixes}
        sp.put(SP_PLAN_MAPPING, mapping)
        yield event.plain_result(f"方案已添加: {plan_id} -> {days}天, 前缀: {', '.join(prefixes)}")

    @filter.command("afdian_delplan")
    async def cmd_delplan(self, event: AstrMessageEvent):
        """删除赞助方案映射"""
        if not await self._check_admin(event):
            yield event.plain_result("无权限")
            return
        parts = event.message_str.strip().split()
        if len(parts) != 3:
            yield event.plain_result("用法: /afdian_delplan <plan_id>")
            return
        plan_id = parts[2]
        mapping = self._get_plan_mapping()
        if plan_id in mapping:
            del mapping[plan_id]
            sp.put(SP_PLAN_MAPPING, mapping)
            yield event.plain_result(f"方案已删除: {plan_id}")
        else:
            yield event.plain_result("方案不存在")

    @filter.command("afdian_addadmin")
    async def cmd_addadmin(self, event: AstrMessageEvent):
        """添加群管理员（静态兜底列表）"""
        if not await self._check_admin(event):
            yield event.plain_result("无权限")
            return
        parts = event.message_str.strip().split()
        if len(parts) != 4:
            yield event.plain_result("用法: /afdian_addadmin <群号> <QQ号>")
            return
        group_id = parts[2]
        qq = parts[3]
        admins = self._get_group_admins()
        if group_id not in admins:
            admins[group_id] = []
        if qq not in admins[group_id]:
            admins[group_id].append(qq)
        sp.put(SP_GROUP_ADMINS, admins)
        yield event.plain_result(f"已添加群{group_id}的管理员: {qq}")

    @filter.command("afdian_deladmin")
    async def cmd_deladmin(self, event: AstrMessageEvent):
        """移除群管理员"""
        if not await self._check_admin(event):
            yield event.plain_result("无权限")
            return
        parts = event.message_str.strip().split()
        if len(parts) != 4:
            yield event.plain_result("用法: /afdian_deladmin <群号> <QQ号>")
            return
        group_id = parts[2]
        qq = parts[3]
        admins = self._get_group_admins()
        if group_id in admins and qq in admins[group_id]:
            admins[group_id].remove(qq)
            if not admins[group_id]:
                del admins[group_id]
            sp.put(SP_GROUP_ADMINS, admins)
            yield event.plain_result(f"已移除群{group_id}的管理员: {qq}")
        else:
            yield event.plain_result("管理员不存在")

    async def _check_admin(self, event: AstrMessageEvent) -> bool:
        try:
            config = getattr(self.context, "astrbot_config", {})
            if not config:
                config = getattr(self.context, "_config", {})
            admins = config.get("admins_id", [])
            return str(event.get_sender_id()) in admins
        except Exception:
            return False

    async def _cron_daily(self):
        while True:
            await asyncio.sleep(self._seconds_until_next_hour(0))
            logger.info("[AfdianModel] 每日零点定时任务开始")
            try:
                active_umos = sp.get(SP_ACTIVE_UMOS, [])
                if not active_umos:
                    logger.info("[AfdianModel] 无活跃绑定，跳过")
                    continue
                now = datetime.now()
                to_remove = []
                for key in list(active_umos):
                    data = sp.get(key, {})
                    if not data or not isinstance(data, dict):
                        to_remove.append(key)
                        continue
                    days = data.get("remaining_days", 0)
                    if days <= 0:
                        to_remove.append(key)
                        continue
                    days -= 1
                    if days <= 0:
                        logger.info(f"[AfdianModel] UMO{key}权限到期，清除绑定")
                        current_key = key + ":current"
                        default_provider = sp.get("curr_provider", "")
                        if sp.get(current_key) and default_provider:
                            try:
                                umo = json.loads(key.replace(SP_UMO_PREFIX, ""))
                                self.context.provider_manager.set_provider(
                                    default_provider, ProviderType.CHAT_COMPLETION, umo
                                )
                            except Exception:
                                pass
                        sp.put(key, None)
                        sp.put(current_key, None)
                        to_remove.append(key)
                    else:
                        data["remaining_days"] = days
                        data["expire_time"] = (now + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                        sp.put(key, data)
                for key in to_remove:
                    if key in active_umos:
                        active_umos.remove(key)
                sp.put(SP_ACTIVE_UMOS, active_umos)
                logger.info("[AfdianModel] 每日零点定时任务完成")
            except Exception as e:
                logger.error(f"[AfdianModel] 每日零点定时任务异常: {e}")

    async def _cron_poll(self):
        await asyncio.sleep(10)
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            api = self._get_api()
            if not api:
                continue
            logger.info("[AfdianModel] 定时轮询开始")
            try:
                page = 1
                while True:
                    resp = await api.query_order(page=page)
                    if resp.get("ec") != 200:
                        break
                    data = resp.get("data", {})
                    orders = data.get("list", [])
                    if not orders:
                        break
                    newest_found = False
                    for order in orders:
                        out_trade_no = order.get("out_trade_no", "")
                        if out_trade_no in self._processed_orders:
                            newest_found = True
                            break
                        self._processed_orders.add(out_trade_no)
                        self._save_processed_orders()
                        if order.get("status") == 2:
                            plan_id = order.get("plan_id", "")
                            plan_mapping = self._get_plan_mapping()
                            if plan_id in plan_mapping:
                                logger.info(f"[AfdianModel] 轮询发现新订单: {out_trade_no}")
                    if newest_found or page >= data.get("total_page", 1):
                        break
                    page += 1
                logger.info("[AfdianModel] 定时轮询完成")
            except Exception as e:
                logger.error(f"[AfdianModel] 定时轮询异常: {e}")

    @staticmethod
    def _seconds_until_next_hour(hour: int) -> float:
        now = datetime.now()
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def terminate(self):
        logger.info("[AfdianModel] 插件已卸载")
