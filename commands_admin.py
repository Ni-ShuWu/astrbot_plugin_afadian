"""核心管理指令 —— reset / reset_all / query / getconfig / setconfig / migrateconfig。"""

import os

from .services import Services
from .storage import StorageManager
from .utils import log_msg


async def cmd_reset(svc: Services, event, is_admin_fn):
    """释放指定订单绑定并扣减余额。"""
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
    if not svc.storage.is_order_processed(order_no):
        yield event.plain_result("该订单未绑定，无需重置")
        return

    api = svc.api_getter()
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
        for o in data.get("list", []):
            if o.get("out_trade_no") == order_no:
                found = o
                break
        if found or pg >= data.get("total_page", 1):
            break
        pg += 1

    if not found:
        yield event.plain_result("订单不存在或查询失败")
        return

    user_id = found.get("user_id", "")
    plan_id = found.get("plan_id", "")
    plan_mapping = svc.plan_manager.get_plan_mapping()

    plan_info = plan_mapping.get(plan_id) or svc.plan_manager.verify_and_get_plan(plan_id)
    if not plan_info:
        plan_days = 0
        plan_level = "1"
    else:
        plan_days = plan_info.get("days", 0)
        cfg = svc.config_fn()
        if plan_id == cfg.get("plan_id_2", "").strip():
            plan_level = "2"
        elif plan_id == cfg.get("plan_id_1", "").strip():
            plan_level = "1"
        else:
            plan_level = plan_info.get("level", "1")

    umo_key = svc.storage.get_user_mapping(user_id)
    deducted_msg = ""
    if umo_key:
        umo_data = svc.sp.get(umo_key, {}) or {}
        if umo_data:
            umo_data = dict(umo_data)
            umo_data = StorageManager.migrate_umo_data(umo_data, svc.wire)

            used_orders = umo_data.get("used_orders", [])
            if order_no in used_orders:
                used_orders.remove(order_no)
                umo_data["used_orders"] = used_orders

            if plan_days > 0:
                if plan_level == "2":
                    old = umo_data.get("l2_days", 0)
                    umo_data["l2_days"] = max(0, old - plan_days)
                    deducted_msg = f"，已扣减 Lv2 {old - umo_data['l2_days']} 天"
                    if umo_data["l2_days"] <= 0 and umo_data.get("l1_days", 0) > 0:
                        umo_data["active_level"] = "1"
                        deducted_msg += "，已切换至 Lv1"
                else:
                    old = umo_data.get("l1_days", 0)
                    umo_data["l1_days"] = max(0, old - plan_days)
                    deducted_msg = f"，已扣减 Lv1 {old - umo_data['l1_days']} 天"

            umo_data["remaining_days"] = umo_data.get("l1_days", 0) + umo_data.get("l2_days", 0)

            if umo_data["remaining_days"] <= 0 or not used_orders:
                umo_data["active_level"] = "0"
                umo_data["level"] = "0"
                umo_data["l1_days"] = 0
                umo_data["l2_days"] = 0
                umo_data["remaining_days"] = 0
                svc.storage.set_umo_data_by_key(umo_key, umo_data)
                svc.storage.unregister_umo(umo_key)
                deducted_msg += "，余额归零已降为 Lv0"
            else:
                svc.storage.set_umo_data_by_key(umo_key, umo_data)

    svc.storage.unmark_order_processed(order_no)
    log_msg(svc.wire, f"订单重置成功: order={order_no} user={user_id}{deducted_msg}")
    yield event.plain_result(f"订单 {order_no} 已重置{deducted_msg}，可以重新绑定")


async def cmd_reset_all(svc: Services, event, is_admin_fn):
    """一键清除所有持久化数据（保留配置）。"""
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
            "• 所有已绑定的订单记录\n• 所有用户的赞助信息\n"
            "• 所有活跃绑定与模型映射\n• 持久化文件（persistence.json）\n\n"
            "**不会清除：**\n• 插件配置文件（模型列表、方案ID等）\n\n"
            "如需执行，请输入：\n`/afdian_reset_all YES`"
        )
        return

    stats = svc.storage.full_reset()
    log_msg(svc.wire, f"一键重置完成: {stats}")
    yield event.plain_result(
        f"✅ **一键重置完成！**\n\n"
        f"已清除：\n"
        f"• {stats['orders']} 条订单记录\n• {stats['umo_data']} 条用户数据\n"
        f"• {stats['user_mappings']} 个用户映射\n• {stats['active_umos']} 个活跃绑定\n"
        f"• persistence.json 持久化文件\n\n插件配置已保留，可正常使用。"
    )


async def cmd_query(svc: Services, event, is_admin_fn):
    """查询指定订单详情。"""
    if not await is_admin_fn(event):
        yield event.plain_result("无权限")
        return

    parts = event.message_str.strip().split()
    if len(parts) < 2:
        yield event.plain_result("用法: /afdian_query <订单号>")
        return

    order_no = parts[1]
    api = svc.api_getter()
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
        if found or pg >= data.get("total_page", 1):
            break
        pg += 1

    if not found:
        yield event.plain_result(f"未找到订单 {order_no}")
        return

    plan_id = found.get("plan_id", "")
    status = found.get("status", 0)
    status_text = {1: "待支付", 2: "已支付", 3: "已退款"}.get(status, f"未知({status})")
    plan_info = ""
    if plan_id:
        plan = svc.plan_manager.verify_and_get_plan(plan_id)
        if plan:
            plan_info = f"\n已配置方案: Lv{plan.get('level')} {plan['days']}天 [{', '.join(plan['prefixes'])}]"
        else:
            plan_info = "\n⚠ 方案未配置"
    yield event.plain_result(
        f"订单查询结果:\n订单号: {order_no}\nplan_id: {plan_id or '无'}\n"
        f"方案名: {found.get('plan_title', '未知')}\n金额: {found.get('total_amount', '')}\n"
        f"状态: {status_text}{plan_info}"
    )


async def cmd_getconfig(svc: Services, event, is_admin_fn):
    """查看当前插件配置。"""
    if not await is_admin_fn(event):
        yield event.plain_result("无权限")
        return
    cfg = svc.config_fn()
    if not cfg:
        yield event.plain_result("当前无配置")
        return
    lines = []
    for k, v in sorted(cfg.items()):
        if "token" in k.lower():
            v = "***"
        lines.append(f"{k}: {v}")
    yield event.plain_result("当前配置:\n" + "\n".join(lines))


async def cmd_setconfig(svc: Services, event, is_admin_fn):
    """设置插件配置项。"""
    if not await is_admin_fn(event):
        yield event.plain_result("无权限")
        return

    parts = event.message_str.strip().split(maxsplit=2)
    if len(parts) < 3:
        yield event.plain_result(
            "用法: /afdian_setconfig <key> <value>\n\n支持的key:\n"
            "- model_list / plan_id_1 / days_1 / models_1\n"
            "- plan_id_2 / days_2 / models_2\n"
            "- afdian_user_id / afdian_token / afdian_api_base"
        )
        return

    key, value = parts[1], parts[2]
    cfg = svc.config_fn()
    cfg[key] = value
    svc.config_manager.save_plugin_config(cfg)
    yield event.plain_result(f"配置已更新: {key} = {value if 'token' not in key.lower() else '***'}")


async def cmd_migrateconfig(svc: Services, event, is_admin_fn):
    """从 AstrBot 旧配置迁移。"""
    if not await is_admin_fn(event):
        yield event.plain_result("无权限")
        return

    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        astrbot_cfg_path = os.path.join(get_astrbot_data_path(), "config", "astrbot_plugin_afdian_model_config.json")

        if os.path.exists(astrbot_cfg_path):
            import json
            with open(astrbot_cfg_path, "r", encoding="utf-8") as f:
                content = f.read()
                if content.startswith("\ufeff"):
                    content = content[1:]
                astrbot_cfg = json.loads(content)
                if astrbot_cfg:
                    svc.config_manager.save_plugin_config(astrbot_cfg)
                    yield event.plain_result(f"配置迁移成功！\n迁移内容: {list(astrbot_cfg.keys())}")
                else:
                    yield event.plain_result("AstrBot 配置为空")
        else:
            yield event.plain_result("未找到 AstrBot 配置文件")
    except Exception as e:
        yield event.plain_result(f"迁移失败: {e}")
