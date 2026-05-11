import os
from .config import ConfigManager
from .storage import StorageManager
from .plan_manager import PlanManager


SP_ACTIVE_UMOS = "afdian_model:active_umos"
SP_UMO_PREFIX = "afdian_model:umo:"
SP_BY_AFDIAN = "afdian_model:by_afdian:"
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, "data")


class AdminCommands:
    def __init__(
        self,
        api_getter,
        config_fn,
        config_manager: ConfigManager,
        storage: StorageManager,
        plan_manager: PlanManager,
        wire_fn
    ):
        self._api_getter = api_getter
        self._config_fn = config_fn
        self._config_manager = config_manager
        self._storage = storage
        self._plan_manager = plan_manager
        self._wire = wire_fn

    async def cmd_reset(self, event, is_admin_fn):
        if not await is_admin_fn(event):
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

        if not self._storage.is_order_processed(order_no):
            yield event.plain_result("该订单未绑定，无需重置")
            return

        api = self._api_getter()
        if not api:
            yield event.plain_result("API未配置，无法验证订单")
            return

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

        umo_key = self._storage.get_user_mapping(user_id)
        if umo_key:
            from astrbot.core import sp
            umo_data = sp.get(umo_key, {})
            if umo_data:
                used_orders = umo_data.get("used_orders", [])
                if order_no in used_orders:
                    used_orders.remove(order_no)
                    umo_data["used_orders"] = used_orders
                    sp.put(umo_key, umo_data)
                    self._wire(f"[AfdianModel] 从用户数据中移除订单: {order_no}")
                if not used_orders:
                    sp.put(umo_key, None)
                    self._storage.unregister_umo(umo_key)
                    self._storage.remove_user_mapping(user_id)
                    self._wire(f"[AfdianModel] 用户数据已销毁: {user_id}")

        self._storage.unmark_order_processed(order_no)

        self._wire(f"[AfdianModel] 订单重置成功: order={order_no} user={user_id}")
        yield event.plain_result(f"订单 {order_no} 已重置，绑定信息已销毁，可以重新绑定")

    async def cmd_reset_all(self, event, is_admin_fn):
        if not await is_admin_fn(event):
            yield event.plain_result("无权限")
            return
        
        if event.get_group_id():
            yield event.plain_result("请在私聊中使用此命令")
            return
        
        parts = event.message_str.strip().split()
        if len(parts) < 2 or parts[1] != "YES":
            yield event.plain_result(
                "⚠️ **警告：此操作将清除所有数据！**\n\n"
                "包括：\n"
                "• 所有已绑定的订单\n"
                "• 所有用户的赞助信息\n"
                "• 所有活跃绑定记录\n\n"
                "**不会清除：**\n"
                "• 插件配置文件\n\n"
                "如需执行，请输入：\n"
                "`/afdian_reset_all YES`"
            )
            return

        from astrbot.core import sp
        active_umos = sp.get(SP_ACTIVE_UMOS, [])
        for umo_key in active_umos:
            sp.put(umo_key, None)
        sp.put(SP_ACTIVE_UMOS, [])

        all_keys = sp.keys()
        user_keys = [k for k in all_keys if k.startswith(SP_UMO_PREFIX)]
        for key in user_keys:
            sp.put(key, None)

        self._storage.clear_orders()

        self._wire(f"[AfdianModel] 一键重置完成")
        yield event.plain_result(
            f"✅ **一键重置完成！**\n\n"
            f"已清除：\n"
            f"• {len(active_umos)} 个活跃绑定\n"
            f"• {len(user_keys)} 个用户数据\n"
            f"• 已处理订单记录\n\n"
            f"插件配置已保留，可正常使用。"
        )

    async def cmd_addmodels(self, event, is_admin_fn):
        if not await is_admin_fn(event):
            yield event.plain_result("无权限")
            return
        
        parts = event.message_str.strip().split(maxsplit=2)
        if len(parts) < 3:
            yield event.plain_result(
                "用法: `/afdian_addmodels <方案等级> <模型名>`\n\n"
                "示例: `/afdian_addmodels 1 openai/gpt-5.4-mini-2026-03-17`\n\n"
                "方案等级: 1 = 赞助方案1, 2 = 赞助方案2"
            )
            return

        level = parts[1]
        model_name = parts[2].strip()

        if level not in ("1", "2"):
            yield event.plain_result("方案等级只能是 1 或 2")
            return

        if not model_name:
            yield event.plain_result("模型名不能为空")
            return

        cfg = self._config_fn()
        models_key = f"models_{level}"
        current_models = cfg.get(models_key, "")
        current_list = self._plan_manager._str_to_list(current_models)

        if model_name in current_list:
            yield event.plain_result(f"模型 `{model_name}` 已在方案{level}中，无需重复添加")
            return

        current_list.append(model_name)
        cfg[models_key] = self._plan_manager._list_to_str(current_list)
        self._config_manager.save_plugin_config(cfg)

        self._plan_manager.sync_plan_mapping()

        plan_id = cfg.get(f"plan_id_{level}", "")

        from astrbot.core import sp
        updated_users = 0
        active_umos = sp.get(SP_ACTIVE_UMOS, [])
        for umo_key in active_umos:
            umo_data = sp.get(umo_key, {})
            if umo_data and umo_data.get("plan_id") == plan_id:
                existing_prefixes = self._plan_manager._str_to_list(umo_data.get("prefixes", ""))
                if model_name not in existing_prefixes:
                    existing_prefixes.append(model_name)
                    umo_data["prefixes"] = self._plan_manager._list_to_str(existing_prefixes)
                    sp.put(umo_key, umo_data)
                    updated_users += 1

        self._wire(f"[AfdianModel] 方案{level}添加模型: {model_name}, 更新了 {updated_users} 个用户")
        yield event.plain_result(
            f"✅ 已将模型添加到方案{level}：\n\n"
            f"**{model_name}**\n\n"
            f"方案{level}当前模型列表：\n" + "\n".join([f"• {m}" for m in current_list]) +
            (f"\n\n已同步到 {updated_users} 个已绑定用户" if updated_users > 0 else "")
        )

    async def cmd_addplan(self, event, is_admin_fn):
        if not await is_admin_fn(event):
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

        from .plan_manager import SP_PLAN_MAPPING
        from astrbot.core import sp
        mapping = sp.get(SP_PLAN_MAPPING, {})
        mapping[plan_id] = {"days": days, "prefixes": self._plan_manager._list_to_str(prefixes)}
        sp.put(SP_PLAN_MAPPING, mapping)
        yield event.plain_result(f"方案已添加: {plan_id} -> {days}天, 前缀: {', '.join(prefixes)}")

    async def cmd_delplan(self, event, is_admin_fn):
        if not await is_admin_fn(event):
            yield event.plain_result("无权限")
            return
        
        parts = event.message_str.strip().split()
        if len(parts) != 2:
            yield event.plain_result("用法: /afdian_delplan <plan_id>")
            return
        
        plan_id = parts[1]
        from .plan_manager import SP_PLAN_MAPPING
        from astrbot.core import sp
        mapping = sp.get(SP_PLAN_MAPPING, {})
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

    async def cmd_query(self, event, is_admin_fn):
        if not await is_admin_fn(event):
            yield event.plain_result("无权限")
            return
        
        parts = event.message_str.strip().split()
        if len(parts) < 2:
            yield event.plain_result("用法: /afdian_query <订单号>")
            return
        
        order_no = parts[1]
        api = self._api_getter()
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
            plan = self._plan_manager.verify_and_get_plan(plan_id)
            if plan:
                plan_info = f"\n已配置方案: Lv{plan.get('level')} {plan['days']}天 [{', '.join(plan['prefixes'])}]"
            else:
                plan_info = "\n⚠️ 方案未配置，请添加到配置"
        yield event.plain_result(
            f"订单查询结果:\n"
            f"订单号: {order_no}\n"
            f"plan_id: {plan_id or '无'}\n"
            f"方案名: {plan_title or '未知'}\n"
            f"金额: {amount}\n"
            f"状态: {status_text}"
            f"{plan_info}"
        )

    async def cmd_getconfig(self, event, is_admin_fn):
        if not await is_admin_fn(event):
            yield event.plain_result("无权限")
            return
        
        cfg = self._config_fn()
        if not cfg:
            yield event.plain_result("当前无配置")
            return
        
        config_lines = []
        for k, v in sorted(cfg.items()):
            if "token" in k.lower():
                v = "***"
            config_lines.append(f"{k}: {v}")
        yield event.plain_result("当前配置:\n" + "\n".join(config_lines))

    async def cmd_setconfig(self, event, is_admin_fn):
        if not await is_admin_fn(event):
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
        cfg = self._config_fn()
        cfg[key] = value
        self._config_manager.save_plugin_config(cfg)
        yield event.plain_result(f"配置已更新: {key} = {value if 'token' not in key.lower() else '***'}")

    async def cmd_migrateconfig(self, event, is_admin_fn):
        if not await is_admin_fn(event):
            yield event.plain_result("无权限")
            return
        
        try:
            plugin_dir = os.path.dirname(DATA_DIR)
            astrbot_data_dir = os.path.dirname(os.path.dirname(plugin_dir))
            astrbot_cfg_path = os.path.join(astrbot_data_dir, "config", "astrbot_plugin_afdian_model_config.json")

            if os.path.exists(astrbot_cfg_path):
                import json
                with open(astrbot_cfg_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    if content.startswith('\ufeff'):
                        content = content[1:]
                    astrbot_cfg = json.loads(content)
                    if astrbot_cfg:
                        self._config_manager.save_plugin_config(astrbot_cfg)
                        yield event.plain_result(f"配置迁移成功！\n迁移内容: {list(astrbot_cfg.keys())}")
                    else:
                        yield event.plain_result("AstrBot 配置为空")
            else:
                yield event.plain_result("未找到 AstrBot 配置文件")
        except Exception as e:
            yield event.plain_result(f"迁移失败: {e}")
