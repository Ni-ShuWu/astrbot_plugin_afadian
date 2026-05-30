"""用户指令 —— help / bind / models / switch / status。"""

from datetime import datetime, timedelta

from .services import Services
from .storage import StorageManager
from .utils import str_to_list


async def cmd_help(svc: Services, event):
    """显示帮助信息。"""
    yield event.plain_result("""**🤖 爱发电赞助插件 - 使用指南**

**🏷️ 身份等级:**
• Lv0（公开）: 未绑定订单，可使用公开模型列表中的模型
• Lv1（一级赞助）: 绑定一级赞助订单后获得，可使用 Lv1 + Lv0 模型
• Lv2（二级赞助）: 绑定二级赞助订单后获得，可使用 Lv2 + Lv1 + Lv0 模型

**📌 用户指令:**
• /afdian_bind <订单号> - 绑定爱发电订单号
• /afdian_models - 查看当前可用模型列表（含编号）
• /afdian_switch <模型名/编号> - 切换当前使用的模型
• /afdian_status - 查看赞助权限状态
• /afdian_help - 显示本帮助信息

**🔧 管理员指令:**
• /afdian_reset <订单号> - 释放指定订单绑定
• /afdian_reset_all YES - ⚠️ 一键清除所有缓存数据
• /afdian_query <订单号> - 查询指定订单详情
• /afdian_addmodels <等级> <模型...> - 批量添加/移动模型
• /afdian_delmodels [模型...] - 批量移除/连通性测试
• /afdian_addplan <plan_id> <天数> <前缀> - 添加赞助方案
• /afdian_delplan <plan_id> - 删除赞助方案
• /afdian_getconfig - 查看插件配置
• /afdian_setconfig <key> <value> - 设置配置项
• /afdian_migrateconfig - 从 AstrBot 配置迁移

**📋 使用流程:**
1. 未绑定用户可直接使用 /afdian_models 查看公开模型
2. 在爱发电赞助并获取订单号
3. 使用 /afdian_bind <订单号> 绑定升级身份
4. 使用 /afdian_models 查看可用模型
5. 使用 /afdian_switch <模型名/编号> 切换模型""")


async def cmd_bind(svc: Services, event):
    """绑定爱发电订单号。"""
    if event.get_group_id():
        yield event.plain_result("请在私聊中使用此命令")
        return

    api = svc.api_getter()
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
    order = next((o for o in order_list if o.get("out_trade_no") == order_no), None)
    total_pages = resp.get("data", {}).get("total_page", 1)
    pg = 2
    while order is None and pg <= total_pages:
        resp = await api.query_order(page=pg)
        if resp.get("ec") != 200:
            break
        order = next((o for o in resp.get("data", {}).get("list", []) if o.get("out_trade_no") == order_no), None)
        pg += 1

    if order is None:
        yield event.plain_result("未找到该订单，请检查订单号是否正确")
        return
    if order.get("status") != 2:
        yield event.plain_result("该订单未支付，无法绑定")
        return
    if svc.storage.is_order_processed(order_no):
        yield event.plain_result("该订单已被使用")
        return

    plan_id = order.get("plan_id", "")
    plan = svc.plan_manager.verify_and_get_plan(plan_id)
    if not plan:
        yield event.plain_result("方案未配置，请联系管理员")
        return

    plan_days = plan.get("days", 0)
    create_time = order.get("create_time", 0)
    if create_time and plan_days > 0:
        order_expire = datetime.fromtimestamp(create_time) + timedelta(days=plan_days)
        if datetime.now() > order_expire:
            yield event.plain_result(f"该订单已超过赞助有效期（{order_expire.strftime('%Y-%m-%d')}），无法绑定")
            return

    svc.storage.mark_order_processed(order_no)

    umo = event.unified_msg_origin
    user_id = order.get("user_id", "")
    existing = svc.storage.get_user_mapping(user_id)
    if existing:
        existing_data = svc.storage.get_umo_data(umo)
        if existing_data and order_no in existing_data.get("used_orders", []):
            svc.storage.unmark_order_processed(order_no)
            yield event.plain_result("该订单已被使用")
            return

    umo_data = await svc.user_manager.bind_user(user_id, plan_id, plan, umo, create_time, order_no)

    model_list = svc.user_manager.get_model_list(svc.config_fn)
    available = []
    for p in str_to_list(umo_data.get("prefixes", "")):
        available.extend(svc.plan_manager.match_prefixes(p, model_list))

    yield event.plain_result(
        f"✅ 绑定成功！\n\n"
        f"📦 方案：Lv{plan.get('level')}（{plan['days']}天）\n"
        f"⏰ 剩余：{umo_data['remaining_days']}天\n"
        f"🤖 可用模型：{', '.join(available) if available else '无'}\n\n"
        f"📋 接下来你可以：\n"
        f"• 使用 /afdian_models 查看可用模型\n"
        f"• 使用 /afdian_status 查看赞助状态"
    )


async def cmd_models(svc: Services, event):
    """查看可用模型列表。"""
    if event.get_group_id():
        yield event.plain_result("请在私聊中使用此命令")
        return

    umo_data = svc.storage.get_umo_data(event.unified_msg_origin)
    level_label, model_id_map, all_models = _get_available_models(svc, umo_data)
    current = svc.storage.get_current_model(event.unified_msg_origin)

    if not all_models:
        yield event.plain_result("当前没有可用模型，请联系管理员添加")
        return

    lines = [f"**可用模型（{level_label}）:**"]
    for m in all_models:
        mid = model_id_map.get(m, "?")
        marker = " ▶ 当前" if m == current else ""
        lines.append(f"  `[{mid}]` {m}{marker}")
    lines.append("\n💡 使用 /afdian_switch <模型名/编号> 切换模型")
    yield event.plain_result("\n".join(lines))


async def cmd_switch(svc: Services, event, is_admin_fn):
    """切换模型。"""
    parts = event.message_str.strip().split(maxsplit=1)
    if len(parts) < 2:
        yield event.plain_result("用法: /afdian_switch <模型名/编号>\n例如: /afdian_switch openai/gpt-5.4-mini-2026-03-17\n或: /afdian_switch zero_1")
        return

    model_input = parts[1].strip()
    umo = event.unified_msg_origin
    umo_data = svc.storage.get_umo_data(umo)
    group_id = event.get_group_id()

    _, model_id_map, _ = _get_available_models(svc, umo_data)
    model_name = next((k for k, v in model_id_map.items() if v == model_input), model_input)

    if not umo_data:
        if group_id:
            yield event.plain_result("你不是爱发电赞助者，无法切换群模型")
            return
        public_models = str_to_list(svc.config_fn().get("model_list", ""))
        if model_name not in public_models:
            ids = "\n".join(f"zero_{i}. {m}" for i, m in enumerate(public_models, 1)) or "无"
            yield event.plain_result(f"无此模型的使用权限\n\n**公开模型 (Lv0):**\n{ids}")
            return
    else:
        user_level = umo_data.get("active_level", umo_data.get("level", "1"))
        if group_id and user_level == "0":
            yield event.plain_result("你的赞助已到期，无法切换群模型")
            return
        has_perm, prefixes = svc.user_manager.has_model_permission(umo_data, model_name)
        if not has_perm:
            ids = "\n".join(f"{i}. {m}" for i, m in enumerate(prefixes, 1)) or "无"
            yield event.plain_result(f"无此模型的使用权限\n\n**可用模型:**\n{ids}")
            return
        if group_id and not await _is_group_admin(svc, event):
            yield event.plain_result("仅群主或群管可切换群模型")
            return

    try:
        from astrbot.core.provider.entities import ProviderType
        await svc.astrbot_context.provider_manager.set_provider(model_name, ProviderType.CHAT_COMPLETION, umo)
    except Exception:
        try:
            await svc.sp.session_put(umo, "provider_perf_chat_completion", model_name)
        except Exception as e:
            yield event.plain_result(f"切换模型失败: {e}")
            return

    svc.storage.set_current_model(umo, model_name)
    yield event.plain_result(f"已切换至 {model_name}")


async def cmd_status(svc: Services, event):
    """查看赞助状态。"""
    if event.get_group_id():
        yield event.plain_result("请在私聊中使用此命令")
        return

    umo_data = svc.storage.get_umo_data(event.unified_msg_origin)
    current = svc.storage.get_current_model(event.unified_msg_origin)

    if not umo_data:
        public_count = len(str_to_list(svc.config_fn().get("model_list", "")))
        yield event.plain_result(
            f"🏷️ 身份等级：Lv0（公开）\n当前模型：{current}\n"
            f"可用模型：{public_count} 个公开模型\n\n"
            "💡 绑定赞助订单可升级为 Lv1/Lv2，获得更多模型权限"
        )
        return

    level = umo_data.get("active_level", umo_data.get("level", "1"))
    l1_stored = umo_data.get("l1_days", 0)
    l2_stored = umo_data.get("l2_days", 0)

    expire_str = umo_data.get("expire_time", "")
    try:
        expire_dt = datetime.strptime(expire_str, "%Y-%m-%d %H:%M:%S")
        remaining_seconds = (expire_dt - datetime.now()).total_seconds()
        if remaining_seconds > 0:
            remain_days = max(0, int(remaining_seconds // 86400))
            remain_hours = int((remaining_seconds % 86400) // 3600)
            remaining_str = f"{remain_days} 天 {remain_hours} 小时"
            if level == "2":
                l1_realtime = l1_stored
                l2_realtime = max(0, remain_days - l1_stored)
            else:
                l2_realtime = l2_stored
                l1_realtime = remain_days
        else:
            remaining_str = "已过期"
            l1_realtime = 0
            l2_realtime = 0
    except (ValueError, OverflowError):
        remaining_str = f"{umo_data.get('remaining_days', 0)} 天"
        l1_realtime = l1_stored
        l2_realtime = l2_stored

    if current == "默认模型":
        prefixes = str_to_list(umo_data.get("prefixes", ""))
        if prefixes:
            current = f"{prefixes[0]}（默认）"

    lines = [
        f"🏷️ 身份等级：Lv{level}",
        f"激活时间：{umo_data.get('order_time', '未知')}",
        f"剩余时间：{remaining_str}",
        f"过期时间：{umo_data.get('expire_time', '未知')}",
    ]
    if l2_realtime > 0:
        marker = " ▶ 消耗中" if level == "2" else "（暂停）"
        lines.append(f"二级余量：{l2_realtime} 天{marker}")
    if l1_realtime > 0:
        marker = " ▶ 消耗中" if level == "1" else "（暂停）"
        lines.append(f"一级余量：{l1_realtime} 天{marker}")

    lines.append(f"\n当前模型：{current}")
    yield event.plain_result("\n".join(lines))


# ── 内部工具 ──────────────────────────────────────

def _get_available_models(svc: Services, umo_data: dict | None) -> tuple[str, dict[str, str], list[str]]:
    """获取用户可用模型（含编号），返回 (等级标签, 编号映射, 模型列表)。"""
    cfg = svc.config_fn()
    model_id_map: dict[str, str] = {}
    all_models: list[str] = []

    if not umo_data:
        public = str_to_list(cfg.get("model_list", ""))
        for i, m in enumerate(public, 1):
            model_id_map[m] = f"zero_{i}"
            all_models.append(m)
        return "Lv0（公开）", model_id_map, all_models

    user_level = umo_data.get("active_level", umo_data.get("level", "1"))
    level_label = f"Lv{user_level}"

    models_2 = str_to_list(cfg.get("models_2", ""))
    models_1 = str_to_list(cfg.get("models_1", ""))
    public = str_to_list(cfg.get("model_list", ""))

    if user_level == "2":
        tiers = [("two", models_2), ("one", models_1), ("zero", public)]
    elif user_level == "1":
        tiers = [("one", models_1), ("zero", public)]
    else:
        tiers = [("zero", public)]
        level_label = "Lv0（公开）"

    seen: set[str] = set()
    for prefix, ms in tiers:
        cnt = 0
        for m in ms:
            if m not in seen:
                cnt += 1
                model_id_map[m] = f"{prefix}_{cnt}"
                all_models.append(m)
                seen.add(m)

    return level_label, model_id_map, all_models


async def _is_group_admin(svc: Services, event) -> bool:
    """检查发送者是否为群主或群管。优先读取平台下发的 group_owner/group_admins。"""
    sender_id = str(event.get_sender_id())
    msg_obj = event.message_obj

    # 平台 API 提供的群角色信息
    if msg_obj.group_owner and sender_id == str(msg_obj.group_owner):
        return True
    if msg_obj.group_admins and sender_id in [str(a) for a in msg_obj.group_admins]:
        return True

    # 静态管理员列表兜底
    cfg = svc.config_fn()
    group_admins = cfg.get("group_admins", {})
    if isinstance(group_admins, dict):
        admins = group_admins.get(str(event.get_group_id()), [])
        return sender_id in admins
    return False
    return False
