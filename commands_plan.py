"""方案管理指令 —— addplan / delplan。"""

from .services import Services
from .storage import StorageManager
from .utils import log_msg


async def cmd_addplan(svc: Services, event, is_admin_fn):
    """添加赞助方案。"""
    if not await is_admin_fn(event):
        yield event.plain_result("无权限")
        return

    parts = event.message_str.strip().split()
    if len(parts) < 4:
        yield event.plain_result("用法: /afdian_addplan <plan_id> <天数> <前缀1,前缀2,...>")
        return

    plan_id = parts[1].strip()
    if not plan_id:
        yield event.plain_result("plan_id 不能为空")
        return
    try:
        days = int(parts[2])
    except ValueError:
        yield event.plain_result("天数必须为整数")
        return
    if days <= 0:
        yield event.plain_result("天数必须大于 0")
        return

    prefixes = [p.strip() for p in parts[3].split(",") if p.strip()]
    if not prefixes:
        yield event.plain_result("至少需要一个模型前缀")
        return

    try:
        mapping = await svc.storage.get_plan_mapping()
        mapping[plan_id] = {"days": days, "prefixes": StorageManager._list_to_str(prefixes)}
        await svc.storage.set_plan_mapping(mapping)
    except Exception as e:
        log_msg(svc.wire, f"添加方案失败: {plan_id} - {e}", "error")
        yield event.plain_result("添加方案失败，请稍后重试（已记录日志）")
        return

    log_msg(svc.wire, f"方案已添加: {plan_id} -> {days}天")
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

    plan_id = parts[1].strip()
    try:
        mapping = await svc.storage.get_plan_mapping()
        auto_key = f"_auto_{plan_id}"
        deleted = False
        if plan_id in mapping:
            del mapping[plan_id]
            deleted = True
        if auto_key in mapping:
            del mapping[auto_key]
            deleted = True
        if deleted:
            await svc.storage.set_plan_mapping(mapping)
        else:
            yield event.plain_result("方案不存在")
            return
    except Exception as e:
        log_msg(svc.wire, f"删除方案失败: {plan_id} - {e}", "error")
        yield event.plain_result("删除方案失败，请稍后重试（已记录日志）")
        return

    log_msg(svc.wire, f"方案已删除: {plan_id}")
    yield event.plain_result(f"方案已删除: {plan_id}")
