from .afdian_api import AfdianAPI
from .config import ConfigManager
from .storage import StorageManager
from .plan_manager import PlanManager
from .user_manager import UserManager


class UserCommands:
    def __init__(
        self,
        api_getter,
        config_fn,
        config_manager: ConfigManager,
        storage: StorageManager,
        plan_manager: PlanManager,
        user_manager: UserManager,
        wire_fn
    ):
        self._api_getter = api_getter
        self._config_fn = config_fn
        self._config_manager = config_manager
        self._storage = storage
        self._plan_manager = plan_manager
        self._user_manager = user_manager
        self._wire = wire_fn

    async def cmd_help(self, event):
        help_text = """**🤖 爱发电赞助插件 - 使用指南**

**📌 用户指令:**
• /afdian_bind <订单号> - 绑定爱发电订单号，获得模型使用权限
• /afdian_models - 查看当前可用的模型列表
• /afdian_switch <模型名> - 切换当前使用的模型
• /afdian_status - 查看赞助权限状态（剩余天数、到期时间等）
• /afdian_help - 显示本帮助信息

**🔧 管理员指令:**
• /afdian_reset <订单号> - 释放指定订单的绑定状态
• /afdian_reset_all YES - ⚠️ 一键清除所有缓存数据
• /afdian_query <订单号> - 查询指定订单详情
• /afdian_addmodels <方案等级> <模型名> - 向方案添加模型(0=公开,1,2)
• /afdian_delmodels [模型名] - 移除模型/可达性测试
• /afdian_addplan <plan_id> <天数> <前缀> - 添加赞助方案
• /afdian_delplan <plan_id> - 删除赞助方案
• /afdian_getconfig - 查看当前插件配置
• /afdian_setconfig <key> <value> - 设置插件配置
• /afdian_migrateconfig - 从 AstrBot 配置迁移

**📋 使用流程:**
1. 在爱发电赞助并获取订单号
2. 使用 /afdian_bind <订单号> 绑定
3. 使用 /afdian_models 查看可用模型
4. 使用 /afdian_switch <模型名> 切换模型"""
        yield event.plain_result(help_text)

    async def cmd_bind(self, event):
        if event.get_group_id():
            yield event.plain_result("请在私聊中使用此命令")
            return
        
        api = self._api_getter()
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
        
        if self._storage.is_order_processed(order_no):
            yield event.plain_result("该订单已被使用")
            return

        plan_id = order.get("plan_id", "")

        plan = self._plan_manager.verify_and_get_plan(plan_id)
        if not plan:
            yield event.plain_result("方案未配置，请联系管理员")
            return

        if self._storage.is_order_processed(order_no):
            self._wire(f"[AfdianModel] 订单{order_no}已在处理列表中，跳过绑定")
            yield event.plain_result("该订单已被使用")
            return

        self._storage.mark_order_processed(order_no)

        umo = event.unified_msg_origin
        create_time = order.get("create_time", 0)

        user_id = order.get("user_id", "")
        existing_umo_key = self._storage.get_user_mapping(user_id)
        if existing_umo_key:
            existing_data = self._storage.get_umo_data(umo)
            if existing_data:
                used_orders = existing_data.get("used_orders", [])
                if order_no in used_orders:
                    self._storage.unmark_order_processed(order_no)
                    self._wire(f"[AfdianModel] 订单{order_no}已在用户数据中，跳过")
                    yield event.plain_result("该订单已被使用")
                    return

        umo_data = await self._user_manager.bind_user(user_id, plan_id, plan, umo, create_time, order_no)

        self._wire(
            f"[AfdianModel] 用户绑定成功: order={order_no} plan={plan_id} level={plan.get('level')} "
            f"days={umo_data['remaining_days']} order_time={umo_data['order_time']} sender={event.get_sender_id()}"
        )

        model_list = self._user_manager.get_model_list(self._config_fn)
        available = []
        for p in umo_data["prefixes"]:
            available.extend(self._plan_manager.match_prefixes(p, model_list))

        yield event.plain_result(
            f"✅ 绑定成功！\n\n"
            f"📦 方案：Lv{plan.get('level')}（{plan['days']}天）\n"
            f"⏰ 剩余：{umo_data['remaining_days']}天\n"
            f"🤖 可用模型：{', '.join(available) if available else '无'}\n\n"
            f"📋 接下来你可以：\n"
            f"• 使用 /afdian_models 查看可用模型\n"
            f"• 使用 /afdian_status 查看赞助状态"
        )

    async def cmd_models(self, event):
        if event.get_group_id():
            yield event.plain_result("请在私聊中使用此命令")
            return
        
        umo_data = self._storage.get_umo_data(event.unified_msg_origin)
        if not umo_data:
            yield event.plain_result("你还没有赞助权限，请先通过爱发电赞助后使用 /afdian_bind <订单号> 绑定")
            return
        
        prefixes = umo_data.get("prefixes", [])
        current = self._storage.get_current_model(event.unified_msg_origin)
        model_lines = []
        for i, model in enumerate(prefixes, 1):
            model_lines.append(f"{i}. {model}")
        result = "**可用模型:**\n" + "\n".join(model_lines) if model_lines else "**可用模型:**\n无"
        result += f"\n\n**当前模型:**{current}"
        result += f"\n\n💡 提示：使用 /afdian_switch <模型名> 切换模型"
        yield event.plain_result(result)

    async def cmd_switch(self, event, is_admin_fn):
        from . import main as main_module

        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result("用法: /afdian_switch <模型名>\n例如: /afdian_switch openai/gpt-5.4-mini-2026-03-17")
            return
        
        model_name = parts[1].strip()
        group_id = event.get_group_id()

        if group_id:
            is_group_admin = await self._is_group_admin(event)
            if not is_group_admin:
                yield event.plain_result("仅群主或群管可切换群模型")
                return

            umo = event.unified_msg_origin
            umo_data = self._storage.get_umo_data(umo)
            if not umo_data:
                yield event.plain_result("你不是爱发电赞助者，无法切换群模型")
                return

            has_permission, prefixes = self._user_manager.has_model_permission(umo_data, model_name)
            if not has_permission:
                model_lines = [f"{i}. {m}" for i, m in enumerate(prefixes, 1)]
                available = "\n".join(model_lines) if model_lines else "无"
                yield event.plain_result(f"无此模型的使用权限\n\n**可用模型:**\n{available}")
                return
        else:
            umo = event.unified_msg_origin
            umo_data = self._storage.get_umo_data(umo)
            if not umo_data:
                yield event.plain_result("无赞助权限，请先绑定")
                return

            has_permission, prefixes = self._user_manager.has_model_permission(umo_data, model_name)
            if not has_permission:
                model_lines = [f"{i}. {m}" for i, m in enumerate(prefixes, 1)]
                available = "\n".join(model_lines) if model_lines else "无"
                yield event.plain_result(f"无此模型的使用权限\n\n**可用模型:**\n{available}")
                return

        try:
            from astrbot.core.provider.entities import ProviderType
            context = event._message_context
            await context.provider_manager.set_provider(
                model_name, ProviderType.CHAT_COMPLETION, umo
            )
        except Exception as e:
            self._wire(f"[AfdianModel] set_provider失败，尝试直写sp.session_put: {e}", "warning")
            try:
                from astrbot.core import sp
                await sp.session_put(
                    umo,
                    "provider_perf_chat_completion",
                    model_name,
                )
            except Exception as e2:
                yield event.plain_result(f"切换模型失败: {e2}")
                return

        self._storage.set_current_model(umo, model_name)
        yield event.plain_result(f"已切换至 {model_name}")

    async def cmd_status(self, event):
        if event.get_group_id():
            yield event.plain_result("请在私聊中使用此命令")
            return
        
        umo_data = self._storage.get_umo_data(event.unified_msg_origin)
        if not umo_data:
            yield event.plain_result("无赞助权限，请先绑定")
            return
        
        current = self._storage.get_current_model(event.unified_msg_origin)
        yield event.plain_result(
            f"下单时间：{umo_data.get('order_time', '未知')}\n"
            f"赞助方案：{umo_data.get('plan_id', '未知')}\n"
            f"剩余天数：{umo_data['remaining_days']}\n"
            f"当前模型：{current}\n"
            f"到期时间：{umo_data.get('expire_time', '未知')}"
        )

    async def _is_group_admin(self, event) -> bool:
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
            self._wire(f"[AfdianModel] 平台API获取群角色失败: {e}", "warning")
        return False
