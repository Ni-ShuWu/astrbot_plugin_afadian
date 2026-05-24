"""方案管理指令 —— addplan / delplan。"""

from .services import Services
from .storage import StorageManager


async def cmd_addplan(svc: Services, event, is_admin_fn):
    """添加赞助方案。"""
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

    mapping = svc.storage.get_plan_mapping()
    mapping[plan_id] = {"days": days, "prefixes": StorageManager._list_to_str(prefixes)}
    svc.storage.set_plan_mapping(mapping)
    yield event.plain_result(f"方案已添加: {plan_id} -> {days}天, 前缀: {', '.join(prefixes)}")


async def cmd_delplan(svc: Services, event, is_admin_fn):
    """删除赞助方案。"""
    if not await is_admin_fn(event):
        yield event.plain_result("无权限")
        return

    parts = event.message_str.strip().split()
    if len(parts) != 2:
        yield event.plain_result("用法: /afdian_delplan <plan_id>")
        return

    plan_id = parts[1]
    mapping = svc.storage.get_plan_mapping()
    auto_key = f"_auto_{plan_id}"
    deleted = False
    if plan_id in mapping:
        del mapping[plan_id]
        deleted = True
    if auto_key in mapping:
        del mapping[auto_key]
        deleted = True
    if deleted:
        svc.storage.set_plan_mapping(mapping)
        yield event.plain_result(f"方案已删除: {plan_id}")
    else:
        yield event.plain_result("方案不存在")
