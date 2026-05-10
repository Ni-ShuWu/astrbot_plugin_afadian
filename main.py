import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core import sp
from astrbot.core.provider.entities import ProviderType

POLL_INTERVAL = 1 * 3600
SP_PLAN_MAPPING = "afdian_model:plan_mapping"
SP_GROUP_ADMINS = "afdian_model:group_admins"
SP_ACTIVE_UMOS = "afdian_model:active_umos"
SP_UMO_PREFIX = "afdian_model:umo:"

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, "data")
ORDERS_PATH = os.path.join(DATA_DIR, "processed_orders.json")
PLUGIN_LOG_PATH = os.path.join(DATA_DIR, "plugin.log")
PLUGIN_CONFIG_PATH = os.path.join(DATA_DIR, "plugin_config.json")


class AfdianAPI:
    def __init__(self, user_id: str, token: str, api_base: str, log_fn=None):
        self._user_id = user_id
        self._token = token
        import re
        self._api_base = re.sub(r"/api/open.*$", "", api_base).rstrip("/")
        self._wire = log_fn or logger.info

    async def query_order(self, page: int = 1) -> dict:
        import aiohttp
        import hashlib
        params = {"page": page}
        ts = int(time.time())
        json_params = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
        raw = f"{self._token}params{json_params}ts{ts}user_id{self._user_id}"
        sig = hashlib.md5(raw.encode()).hexdigest()
        body = {
            "user_id": self._user_id,
            "params": json_params,
            "ts": ts,
            "sign": sig,
        }
        url = f"{self._api_base}/api/open/query-order"
        try:
            self._wire(f"[AfdianModel] API请求: query-order page={page} url={url}")
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    data = await resp.json(content_type=None)
                    ec = data.get("ec", -1)
                    if ec == 200:
                        order_count = len(data.get("data", {}).get("list", []))
                        self._wire(f"[AfdianModel] API响应: query-order page={page} ec=200 orders={order_count}")
                    else:
                        self._wire(f"[AfdianModel] API响应异常: query-order page={page} ec={ec} em={data.get('em', '')}", "warning")
                    return data
        except Exception as e:
            self._wire(f"[AfdianModel] API请求失败: query-order page={page} - {e}", "error")
            return {"ec": -1, "em": str(e)}


class AfdianModelPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self._api = None
        self._processed_orders = set()
        self._context = context
        self._star_config = config if config else {}
        self._init_data_dir()
        self._processed_orders = self._load_processed_orders()
        self._sync_plan_mapping()

        asyncio.create_task(self._cron_daily())
        asyncio.create_task(self._cron_poll())

    def _init_data_dir(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            fh = RotatingFileHandler(
                PLUGIN_LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter(
                "[%(asctime)s] [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"
            ))
            plog = logging.getLogger("afdian_model")
            plog.addHandler(fh)
            plog.setLevel(logging.DEBUG)
            self._plog = plog
        except Exception:
            self._plog = logger

    def _wire(self, msg: str, level: str = "info"):
        getattr(logger, level)(msg)
        getattr(self._plog, level)(msg)
    
    def _load_plugin_config(self) -> dict:
        """加载插件自己的配置文件"""
        try:
            if os.path.exists(PLUGIN_CONFIG_PATH):
                with open(PLUGIN_CONFIG_PATH, "r", encoding="utf-8") as f:
                    # 使用 utf-8-sig 处理 BOM
                    content = f.read()
                    if content.startswith('\ufeff'):
                        content = content[1:]
                    cfg = json.loads(content)
                    self._wire(f"[AfdianModel] 插件配置加载成功: {list(cfg.keys())}", "info")
                    return cfg
        except Exception as e:
            self._wire(f"[AfdianModel] 插件配置加载失败: {e}", "error")
        return {}
    
    def _save_plugin_config(self, cfg: dict):
        """保存插件自己的配置文件"""
        try:
            with open(PLUGIN_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            self._wire(f"[AfdianModel] 插件配置保存成功: {list(cfg.keys())}", "info")
        except Exception as e:
            self._wire(f"[AfdianModel] 插件配置保存失败: {e}", "error")

    def _get_api(self):
        cfg = self._config()
        uid = cfg.get("afdian_user_id", "")
        token = cfg.get("afdian_token", "")
        api_base = cfg.get("afdian_api_base", "https://afdian.net")
        self._wire(
            f"[AfdianModel] API配置检查: user_id={'***' if uid else 'EMPTY'} "
            f"token={'***' if token else 'EMPTY'} base={api_base}",
            "info"
        )
        if not uid or not token:
            self._wire(
                f"[AfdianModel] API未配置: user_id={'***' if uid else 'EMPTY'} "
                f"token={'***' if token else 'EMPTY'}",
                "warning"
            )
            return None
        try:
            # 每次都重新创建 API 实例，确保使用最新配置
            self._api = AfdianAPI(uid, token, api_base, self._wire)
            self._wire("[AfdianModel] API初始化成功")
        except Exception as e:
            self._wire(f"[AfdianModel] API初始化失败: {e}", "error")
            return None
        return self._api

    def _config(self) -> dict:
        """获取配置，优先使用插件自己的配置文件"""
        try:
            # 优先加载插件自己的配置
            plugin_cfg = self._load_plugin_config()
            if plugin_cfg:
                return plugin_cfg
            
            # 如果插件配置不存在，尝试加载 AstrBot 的配置作为初始配置
            try:
                plugin_dir = os.path.dirname(DATA_DIR)
                astrbot_data_dir = os.path.dirname(os.path.dirname(plugin_dir))
                
                astrbot_cfg_path = os.path.join(astrbot_data_dir, "config", "astrbot_plugin_afdian_model_config.json")
                if os.path.exists(astrbot_cfg_path):
                    self._wire(f"[AfdianModel] 尝试从 AstrBot 配置迁移: {astrbot_cfg_path}", "info")
                    with open(astrbot_cfg_path, "r", encoding="utf-8") as f:
                        content = f.read()
                        if content.startswith('\ufeff'):
                            content = content[1:]
                        astrbot_cfg = json.loads(content)
                        if astrbot_cfg:
                            self._save_plugin_config(astrbot_cfg)
                            return astrbot_cfg
            except Exception as e:
                self._wire(f"[AfdianModel] 迁移 AstrBot 配置失败: {e}", "warning")
            
            # 如果还是没有，使用初始化配置
            cfg = self._star_config if isinstance(self._star_config, dict) and self._star_config else {}
            if cfg:
                self._save_plugin_config(cfg)
            self._wire(f"[AfdianModel] 使用初始化配置: keys={list(cfg.keys()) if cfg else 'EMPTY'}", "info")
            return cfg
        except Exception as e:
            self._wire(f"[AfdianModel] 配置读取异常: {e}", "error")
            return {}

    def _get_model_list(self) -> list:
        model_list_raw = self._config().get("model_list", "")
        model_list = self._parse_models(model_list_raw)
        self._wire(f"[AfdianModel] _get_model_list: raw='{model_list_raw}' parsed={model_list}", "info")
        return model_list

    def _parse_models(self, raw) -> list:
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str) and raw.strip():
            return [m.strip() for m in raw.split(",") if m.strip()]
        return []
    
    def _list_to_str(self, lst: list) -> str:
        if not lst:
            return ""
        return ",".join(lst)
    
    def _str_to_list(self, s: str) -> list:
        if not s or not isinstance(s, str):
            return []
        return [item.strip() for item in s.split(",") if item.strip()]

    def _sync_plan_mapping(self):
        # 每次同步时都重新读取配置
        existing = {}
        # 先从持久化存储中读取已有的映射（保留管理员手动添加的）
        stored = sp.get(SP_PLAN_MAPPING, {})
        if stored:
            # 只保留管理员手动添加的方案（不在自动配置范围内的）
            for plan_id, plan_data in stored.items():
                if not plan_id.startswith("_auto_"):  # 标记自动配置的方案
                    existing[plan_id] = plan_data
        
        updated = False
        for level in ("1", "2"):
            plan_id = self._config().get(f"plan_id_{level}", "").strip()
            prefixes = self._parse_models(self._config().get(f"models_{level}", ""))
            if not plan_id or not prefixes:
                continue
            days = self._config().get(f"days_{level}", 30 if level == "1" else 365)
            # 使用特殊前缀标记自动配置的方案
            existing[f"_auto_{plan_id}"] = {"days": days, "prefixes": self._list_to_str(prefixes)}
            updated = True
            self._wire(f"[AfdianModel] 自动绑定 Lv{level}方案: {plan_id} -> {days}天 [{', '.join(prefixes)}]")
        if updated:
            sp.put(SP_PLAN_MAPPING, existing)
    
    def _get_plan_mapping(self) -> dict:
        mapping = sp.get(SP_PLAN_MAPPING, {})
        result = {}
        for plan_id, plan_data in mapping.items():
            # 去掉自动配置方案的前缀
            clean_id = plan_id[6:] if plan_id.startswith("_auto_") else plan_id
            result[clean_id] = {
                "days": plan_data.get("days", 0),
                "prefixes": self._str_to_list(plan_data.get("prefixes", ""))
            }
        return result
    
    def _verify_and_get_plan(self, order_plan_id: str) -> dict | None:
        """
        直接从配置中验证订单的plan_id，返回匹配的方案信息
        避免依赖持久化存储的映射
        """
        self._wire(f"[AfdianModel] 验证plan_id: {order_plan_id}", "info")
        
        # 直接从配置读取 plan_id_1 和 plan_id_2
        plan_id_1 = self._config().get("plan_id_1", "").strip()
        plan_id_2 = self._config().get("plan_id_2", "").strip()
        
        self._wire(f"[AfdianModel] 配置中的plan_id_1: {plan_id_1}", "info")
        self._wire(f"[AfdianModel] 配置中的plan_id_2: {plan_id_2}", "info")
        
        # 检查是否匹配 plan_id_1
        if plan_id_1 and order_plan_id == plan_id_1:
            days = self._config().get("days_1", 30)
            prefixes = self._parse_models(self._config().get("models_1", ""))
            self._wire(f"[AfdianModel] 匹配到Lv1方案: days={days}, prefixes={prefixes}", "info")
            return {"days": days, "prefixes": prefixes, "level": "1"}
        
        # 检查是否匹配 plan_id_2
        if plan_id_2 and order_plan_id == plan_id_2:
            days = self._config().get("days_2", 365)
            prefixes = self._parse_models(self._config().get("models_2", ""))
            self._wire(f"[AfdianModel] 匹配到Lv2方案: days={days}, prefixes={prefixes}", "info")
            return {"days": days, "prefixes": prefixes, "level": "2"}
        
        self._wire(f"[AfdianModel] 未匹配到任何方案", "warning")
        return None

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
            self._wire(f"[AfdianModel] 保存订单记录失败: {e}", "error")

    def _get_group_admins(self) -> dict:
        admins = sp.get(SP_GROUP_ADMINS, {})
        result = {}
        for group_id, admin_str in admins.items():
            result[group_id] = self._str_to_list(admin_str)
        return result

    def _umo_key(self, umo) -> str:
        return f"{SP_UMO_PREFIX}{json.dumps(umo, separators=(',', ':'), sort_keys=True)}"

    def _get_umo_data(self, umo) -> dict:
        data = sp.get(self._umo_key(umo), {})
        if data:
            # 转换 prefixes 为 list 供代码使用
            data = dict(data)  # 复制一份，避免修改原始数据
            data["prefixes"] = self._str_to_list(data.get("prefixes", ""))
        return data

    def _set_umo_data(self, umo, data: dict):
        sp.put(self._umo_key(umo), data)

    def _match_prefixes(self, prefix: str, model_list: list) -> list:
        """匹配模型：精确匹配 + 前缀匹配"""
        matched = []
        for m in model_list:
            # 精确匹配
            if m == prefix:
                matched.append(m)
            # 前缀匹配（model 以 prefix 开头）
            elif m.startswith(prefix):
                matched.append(m)
            # 部分匹配（prefix 以 model 开头，即 prefix 更长）
            elif prefix.startswith(m) and len(m) > 0:
                matched.append(m)
        return matched

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
            self._wire(f"[AfdianModel] 平台API获取群角色失败，使用静态列表兜底: {e}", "debug")
        group_admins = self._get_group_admins()
        allowed = group_admins.get(str(group_id), [])
        return str(sender_id) in allowed

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
            self._wire(f"[AfdianModel] 订单{out_trade_no}未支付，状态: {status}")
            return

        plan_id = order.get("plan_id", "")
        plan_mapping = self._get_plan_mapping()
        if plan_id not in plan_mapping:
            self._wire(f"[AfdianModel] 方案{plan_id}未配置，订单{out_trade_no}")
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
                # 合并 prefixes 并去重
                existing_prefixes = self._str_to_list(umo_data.get("prefixes", ""))
                combined_prefixes = list(set(existing_prefixes + prefixes))
                umo_data["prefixes"] = self._list_to_str(combined_prefixes)
                umo_data["expire_time"] = (
                    datetime.now() + timedelta(days=umo_data["remaining_days"])
                ).strftime("%Y-%m-%d %H:%M:%S")
                sp.put(umo_key, umo_data)
                self._wire(
                    f"[AfdianModel] 用户{user_id}累加{days}天，剩余{umo_data['remaining_days']}天 "
                    f"订单{order.get('out_trade_no','')} 下单时间"
                    f"@{datetime.fromtimestamp(order.get('create_time',0)).strftime('%Y-%m-%d %H:%M:%S') if order.get('create_time') else '未知'}"
                )

    async def _bind_user(self, user_id: str, plan_id: str, plan: dict, umo, create_time: int = 0):
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
                                "prefixes": old_data.get("prefixes", ""),
                                "expire_time": old_data.get("expire_time", ""),
                                "plan_id": old_data.get("plan_id", ""),
                                "order_time": old_data.get("order_time", "")}
                self._set_umo_data(umo, new_data)
                sp.put(existing, None)
                self._unregister_umo(existing)
        umo_data = self._get_umo_data(umo)
        order_time = datetime.fromtimestamp(create_time).strftime("%Y-%m-%d %H:%M:%S") if create_time else "未知"
        if umo_data:
            umo_data["remaining_days"] += days
            # 合并 prefixes 并去重
            existing_prefixes = self._str_to_list(umo_data.get("prefixes", ""))
            combined_prefixes = list(set(existing_prefixes + prefixes))
            umo_data["prefixes"] = self._list_to_str(combined_prefixes)
        else:
            umo_data = {"remaining_days": days, "prefixes": self._list_to_str(prefixes), "plan_id": plan_id}
        umo_data["order_time"] = order_time
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
        if len(parts) < 2:
            yield event.plain_result("用法: /afdian_bind <订单号>")
            return
        order_no = parts[1]
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
        
        # 使用新的验证函数直接匹配
        plan = self._verify_and_get_plan(plan_id)
        if not plan:
            yield event.plain_result("方案未配置，请联系管理员")
            return

        self._processed_orders.add(order_no)
        self._save_processed_orders()

        umo = event.unified_msg_origin
        create_time = order.get("create_time", 0)
        umo_data = await self._bind_user(order.get("user_id", ""), plan_id, plan, umo, create_time)

        self._wire(
            f"[AfdianModel] 用户绑定成功: order={order_no} plan={plan_id} level={plan.get('level')} "
            f"days={umo_data['remaining_days']} order_time={umo_data['order_time']} sender={event.get_sender_id()}"
        )

        model_list = self._get_model_list()
        available = []
        for p in umo_data["prefixes"]:
            available.extend(self._match_prefixes(p, model_list))

        yield event.plain_result(
            f"绑定成功，方案：Lv{plan.get('level')}（{plan['days']}天），"
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
        prefixes = umo_data.get("prefixes", [])
        current = sp.get(self._umo_key(event.unified_msg_origin) + ":current", "默认模型")
        yield event.plain_result(
            f"可用模型：{', '.join(prefixes) if prefixes else '无'}\n当前模型：{current}"
        )

    @filter.command("afdian_switch")
    async def cmd_switch(self, event: AstrMessageEvent):
        """切换当前使用的LLM模型，私聊切换个人模型，群聊切换全群模型（需群管权限）"""
        parts = event.message_str.strip().split()
        if len(parts) < 3:
            yield event.plain_result("用法: /afdian_switch <前缀> <模型名>")
            return
        prefix = parts[1]
        model_name = parts[2]
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

        model_list = self._get_model_list()
        matched = self._match_prefixes(prefix, model_list)
        if model_name not in matched:
            yield event.plain_result(f"模型{model_name}不在可用列表中，可用: {', '.join(matched) if matched else '无'}")
            return

        try:
            self.context.provider_manager.set_provider(
                model_name, ProviderType.CHAT_COMPLETION, umo
            )
        except Exception as e:
            self._wire(f"[AfdianModel] set_provider失败，尝试备用方式: {e}", "warning")
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
            f"下单时间：{umo_data.get('order_time', '未知')}\n"
            f"赞助方案：{umo_data.get('plan_id', '未知')}\n"
            f"剩余天数：{umo_data['remaining_days']}\n"
            f"当前模型：{current}\n"
            f"到期时间：{umo_data.get('expire_time', '未知')}"
        )

    @filter.command("afdian_reset")
    async def cmd_reset(self, event: AstrMessageEvent):
        """释放指定订单的绑定状态，销毁激活信息"""
        if not await self._check_admin(event):
            yield event.plain_result("无权限")
            return
        if event.get_group_id():
            yield event.plain_result("请在私聊中使用此命令")
            return
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result("用法: /afdian_reset <订单号>")
            return
        order_no = parts[1]
        
        # 检查订单是否在已处理列表中
        if order_no not in self._processed_orders:
            yield event.plain_result("该订单未绑定，无需重置")
            return
        
        # 查询订单获取用户信息
        api = self._get_api()
        if not api:
            yield event.plain_result("API未配置，无法验证订单")
            return
        
        # 搜索订单
        found = None
        pg = 1
        while True:
            resp = await api.query_order(page=pg)
            if resp.get("ec") != 200:
                break
            data = resp.get("data", {})
            orders = data.get("list", [])
            for o in orders:
                if o.get("out_trade_no") == order_no:
                    found = o
                    break
            if found:
                break
            if pg >= data.get("total_page", 1):
                break
            pg += 1
        
        if not found:
            yield event.plain_result("订单不存在或查询失败")
            return
        
        user_id = found.get("user_id", "")
        
        # 查找并销毁用户数据
        umo_key = sp.get(f"{SP_UMO_PREFIX}by_afdian:{user_id}", None)
        if umo_key:
            # 销毁用户数据
            sp.put(umo_key, None)
            # 从活跃列表移除
            self._unregister_umo(umo_key)
            # 删除用户映射
            sp.put(f"{SP_UMO_PREFIX}by_afdian:{user_id}", None)
        
        # 从已处理订单中移除
        self._processed_orders.discard(order_no)
        self._save_processed_orders()
        
        self._wire(f"[AfdianModel] 订单重置成功: order={order_no} user={user_id}")
        yield event.plain_result(f"订单 {order_no} 已重置，绑定信息已销毁，可以重新绑定")

    # ==================== 管理员命令 ====================

    @filter.command("afdian_addplan")
    async def cmd_addplan(self, event: AstrMessageEvent):
        """添加赞助方案映射：plan_id -> 天数 + 模型前缀"""
        if not await self._check_admin(event):
            yield event.plain_result("无权限")
            return
        parts = event.message_str.strip().split()
        if len(parts) < 4:
            yield event.plain_result("用法: /afdian_addplan <plan_id> <天数> <前缀1,前缀2,...>")
            return
        plan_id = parts[1]
        try:
            days = int(parts[2])
        except ValueError:
            yield event.plain_result("天数必须为整数")
            return
        prefixes = [p.strip() for p in parts[3].split(",") if p.strip()]
        if not prefixes:
            yield event.plain_result("至少需要一个模型前缀")
            return
        # 获取原始的 mapping
        mapping = sp.get(SP_PLAN_MAPPING, {})
        mapping[plan_id] = {"days": days, "prefixes": self._list_to_str(prefixes)}
        sp.put(SP_PLAN_MAPPING, mapping)
        yield event.plain_result(f"方案已添加: {plan_id} -> {days}天, 前缀: {', '.join(prefixes)}")

    @filter.command("afdian_delplan")
    async def cmd_delplan(self, event: AstrMessageEvent):
        """删除赞助方案映射"""
        if not await self._check_admin(event):
            yield event.plain_result("无权限")
            return
        parts = event.message_str.strip().split()
        if len(parts) != 2:
            yield event.plain_result("用法: /afdian_delplan <plan_id>")
            return
        plan_id = parts[1]
        mapping = sp.get(SP_PLAN_MAPPING, {})
        # 检查是否有 _auto_ 前缀的版本
        auto_key = f"_auto_{plan_id}"
        deleted = False
        if plan_id in mapping:
            del mapping[plan_id]
            deleted = True
        if auto_key in mapping:
            del mapping[auto_key]
            deleted = True
        if deleted:
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
        if len(parts) != 3:
            yield event.plain_result("用法: /afdian_addadmin <群号> <QQ号>")
            return
        group_id = parts[1]
        qq = parts[2]
        # 获取原始的 admins 数据
        admins = sp.get(SP_GROUP_ADMINS, {})
        admin_list = self._str_to_list(admins.get(group_id, ""))
        if qq not in admin_list:
            admin_list.append(qq)
        admins[group_id] = self._list_to_str(admin_list)
        sp.put(SP_GROUP_ADMINS, admins)
        yield event.plain_result(f"已添加群{group_id}的管理员: {qq}")

    @filter.command("afdian_deladmin")
    async def cmd_deladmin(self, event: AstrMessageEvent):
        """移除群管理员"""
        if not await self._check_admin(event):
            yield event.plain_result("无权限")
            return
        parts = event.message_str.strip().split()
        if len(parts) != 3:
            yield event.plain_result("用法: /afdian_deladmin <群号> <QQ号>")
            return
        group_id = parts[1]
        qq = parts[2]
        # 获取原始的 admins 数据
        admins = sp.get(SP_GROUP_ADMINS, {})
        admin_list = self._str_to_list(admins.get(group_id, ""))
        if qq in admin_list:
            admin_list.remove(qq)
            if not admin_list:
                del admins[group_id]
            else:
                admins[group_id] = self._list_to_str(admin_list)
            sp.put(SP_GROUP_ADMINS, admins)
            yield event.plain_result(f"已移除群{group_id}的管理员: {qq}")
        else:
            yield event.plain_result("管理员不存在")

    @filter.command("afdian_query")
    async def cmd_query(self, event: AstrMessageEvent):
        """管理员查询指定订单号的plan_id"""
        if not await self._check_admin(event):
            yield event.plain_result("无权限")
            return
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result("用法: /afdian_query <订单号>")
            return
        order_no = parts[1]
        api = self._get_api()
        if not api:
            yield event.plain_result("API未配置")
            return
        yield event.plain_result(f"正在查询订单 {order_no} ...")
        pg = 1
        found = None
        while True:
            resp = await api.query_order(page=pg)
            if resp.get("ec") != 200:
                yield event.plain_result("API查询失败")
                return
            data = resp.get("data", {})
            orders = data.get("list", [])
            if not orders:
                break
            for o in orders:
                if o.get("out_trade_no") == order_no:
                    found = o
                    break
            if found:
                break
            if pg >= data.get("total_page", 1):
                break
            pg += 1
        if not found:
            yield event.plain_result(f"未找到订单 {order_no}")
            return
        plan_id = found.get("plan_id", "")
        plan_title = found.get("plan_title", "")
        amount = found.get("total_amount", "")
        status = found.get("status", 0)
        status_text = {1: "待支付", 2: "已支付", 3: "已退款"}.get(status, f"未知({status})")
        plan_info = ""
        if plan_id:
            # 使用新的验证函数
            plan = self._verify_and_get_plan(plan_id)
            if plan:
                plan_info = f"\n已配置方案: Lv{plan.get('level')} {plan['days']}天 [{', '.join(plan['prefixes'])}]"
            else:
                plan_info = "\n⚠ 方案未配置，请添加到配置"
        yield event.plain_result(
            f"订单查询结果:\n"
            f"订单号: {order_no}\n"
            f"plan_id: {plan_id or '无'}\n"
            f"方案名: {plan_title or '未知'}\n"
            f"金额: {amount}\n"
            f"状态: {status_text}"
            f"{plan_info}"
        )
    
    @filter.command("afdian_getconfig")
    async def cmd_getconfig(self, event: AstrMessageEvent):
        """查看当前配置（管理员）"""
        if not await self._check_admin(event):
            yield event.plain_result("无权限")
            return
        cfg = self._config()
        if not cfg:
            yield event.plain_result("当前无配置")
            return
        config_lines = []
        for k, v in sorted(cfg.items()):
            if "token" in k.lower():
                v = "***"
            config_lines.append(f"{k}: {v}")
        yield event.plain_result("当前配置:\n" + "\n".join(config_lines))
    
    @filter.command("afdian_setconfig")
    async def cmd_setconfig(self, event: AstrMessageEvent):
        """设置配置项（管理员）: /afdian_setconfig <key> <value>"""
        if not await self._check_admin(event):
            yield event.plain_result("无权限")
            return
        parts = event.message_str.strip().split(maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result("用法: /afdian_setconfig <key> <value>\n\n支持的key:\n"
                                      "- model_list: 模型列表，逗号分隔\n"
                                      "- plan_id_1: 第一级方案ID\n"
                                      "- days_1: 第一级天数\n"
                                      "- models_1: 第一级模型前缀，逗号分隔\n"
                                      "- plan_id_2: 第二级方案ID\n"
                                      "- days_2: 第二级天数\n"
                                      "- models_2: 第二级模型前缀，逗号分隔\n"
                                      "- afdian_user_id: 爱发电用户ID\n"
                                      "- afdian_token: 爱发电API Token\n"
                                      "- afdian_api_base: 爱发电API地址（可选）")
            return
        key = parts[1]
        value = parts[2]
        cfg = self._config()
        cfg[key] = value
        self._save_plugin_config(cfg)
        yield event.plain_result(f"配置已更新: {key} = {value if 'token' not in key.lower() else '***'}")
    
    @filter.command("afdian_migrateconfig")
    async def cmd_migrateconfig(self, event: AstrMessageEvent):
        """从 AstrBot 配置迁移到插件配置（管理员）"""
        if not await self._check_admin(event):
            yield event.plain_result("无权限")
            return
        try:
            plugin_dir = os.path.dirname(DATA_DIR)
            astrbot_data_dir = os.path.dirname(os.path.dirname(plugin_dir))
            astrbot_cfg_path = os.path.join(astrbot_data_dir, "config", "astrbot_plugin_afdian_model_config.json")
            
            if os.path.exists(astrbot_cfg_path):
                with open(astrbot_cfg_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    if content.startswith('\ufeff'):
                        content = content[1:]
                    astrbot_cfg = json.loads(content)
                    if astrbot_cfg:
                        self._save_plugin_config(astrbot_cfg)
                        yield event.plain_result(f"配置迁移成功！\n迁移内容: {list(astrbot_cfg.keys())}")
                    else:
                        yield event.plain_result("AstrBot 配置为空")
            else:
                yield event.plain_result("未找到 AstrBot 配置文件")
        except Exception as e:
            yield event.plain_result(f"迁移失败: {e}")

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
            try:
                active_umos = sp.get(SP_ACTIVE_UMOS, [])
                total = len(active_umos)
                if not active_umos:
                    self._wire("[AfdianModel] Daily OK | Bindings: 0 active, nothing to do")
                    continue
                now = datetime.now()
                to_remove = []
                expired = 0
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
                        self._wire(f"[AfdianModel] UMO{key}权限到期，清除绑定")
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
                        expired += 1
                    else:
                        data["remaining_days"] = days
                        data["expire_time"] = (now + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                        sp.put(key, data)
                for key in to_remove:
                    if key in active_umos:
                        active_umos.remove(key)
                sp.put(SP_ACTIVE_UMOS, active_umos)
                remaining = len(active_umos)
                self._wire(
                    f"[AfdianModel] Daily OK | Bindings: {total} -> {remaining} | "
                    f"Expired: {expired} cleaned"
                )
            except Exception as e:
                self._wire(f"[AfdianModel] 每日零点定时任务异常: {e}", "error")

    async def _cron_poll(self):
        await asyncio.sleep(5)
        while True:
            api = self._get_api()
            if not api:
                self._wire("[AfdianModel] Poll SKIP | API未配置，等待下次尝试", "warning")
                await asyncio.sleep(POLL_INTERVAL)
                continue
            try:
                page = 1
                total_scanned = 0
                new_orders = 0
                while True:
                    resp = await api.query_order(page=page)
                    if resp.get("ec") != 200:
                        break
                    data = resp.get("data", {})
                    orders = data.get("list", [])
                    if not orders:
                        break
                    total_scanned += len(orders)
                    newest_found = False
                    for order in orders:
                        out_trade_no = order.get("out_trade_no", "")
                        if out_trade_no in self._processed_orders:
                            newest_found = True
                            break
                        new_orders += 1
                        await self._process_single_order(order)
                    if newest_found:
                        break
                    if page >= data.get("total_page", 1):
                        break
                    page += 1
                umo_count = len(sp.get(SP_ACTIVE_UMOS, []))
                self._wire(
                    f"[AfdianModel] Poll OK | Orders: {new_orders} new / {total_scanned} scanned | "
                    f"Bindings: {umo_count} active"
                )
            except Exception as e:
                self._wire(f"[AfdianModel] 定时轮询异常: {e}", "error")
            await asyncio.sleep(POLL_INTERVAL)

    @staticmethod
    def _seconds_until_next_hour(hour: int) -> float:
        now = datetime.now()
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def terminate(self):
        self._wire("[AfdianModel] 插件已卸载")
