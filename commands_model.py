"""模型管理指令 —— addmodels / delmodels。"""

import asyncio
from datetime import datetime

from .services import Services
from .storage import SP_UMO_PREFIX, StorageManager
from .utils import LEVEL_ID_PREFIX, log_msg, model_names_from_config, str_to_list


async def cmd_addmodels(svc: Services, event, is_admin_fn):
    """批量添加模型到指定等级，已存在则从其他等级迁移。"""
    if not await is_admin_fn(event):
        yield event.plain_result("无权限")
        return

    parts = event.message_str.strip().split()
    if len(parts) < 3:
        yield event.plain_result(
            "用法: `/afdian_addmodels <方案等级> <模型名...>`\n\n"
            "单个: `/afdian_addmodels 1 openai/gpt-5.4-mini-2026-03-17`\n"
            "批量: `/afdian_addmodels 1 模型一 模型二 模型三`\n\n"
            "方案等级: 0 = 公开模型, 1 = 赞助方案1, 2 = 赞助方案2\n\n"
            "💡 若模型已在其他等级中，将自动移动到目标等级"
        )
        return

    target_level = parts[1]
    model_names = [m.strip() for m in parts[2:] if m.strip()]

    if target_level not in ("0", "1", "2"):
        yield event.plain_result("方案等级只能是 0(公开), 1 或 2")
        return
    if not model_names:
        yield event.plain_result("至少需要一个模型名")
        return

    cfg = svc.config_fn()
    model_list = str_to_list(cfg.get("model_list", ""))
    models_1 = str_to_list(cfg.get("models_1", ""))
    models_2 = str_to_list(cfg.get("models_2", ""))

    LEVEL_LISTS = {"0": model_list, "1": models_1, "2": models_2}
    LEVEL_NAMES = {"0": "公开(Lv0)", "1": "方案1(Lv1)", "2": "方案2(Lv2)"}
    target_list = LEVEL_LISTS[target_level]

    moved: list[tuple[str, list[str], str]] = []
    added_new: list[str] = []
    skipped: list[str] = []

    for mn in model_names:
        if mn in target_list:
            skipped.append(mn)
            continue
        from_levels = [lvl for lvl, lst in LEVEL_LISTS.items() if lvl != target_level and mn in lst]
        for lvl in from_levels:
            LEVEL_LISTS[lvl].remove(mn)
        target_list.append(mn)
        if from_levels:
            moved.append((mn, from_levels, target_level))
        else:
            added_new.append(mn)

    if not moved and not added_new:
        yield event.plain_result(f"所有模型已在{LEVEL_NAMES[target_level]}中: {', '.join(model_names)}")
        return

    from .storage import StorageManager as _SM
    cfg["model_list"] = _SM._list_to_str(model_list)
    cfg["models_1"] = _SM._list_to_str(models_1)
    cfg["models_2"] = _SM._list_to_str(models_2)
    svc.config_manager.save_plugin_config(cfg)
    svc.plan_manager.sync_plan_mapping()

    all_moved = {mn for mn, _, _ in moved}
    all_affected = all_moved | set(added_new)

    updated_users = 0
    reset_users = 0
    active_umos = svc.storage.get_active_umos()

    async with svc.storage._lock:
        for umo_key in active_umos:
            umo_data = svc.storage.get_umo_data_by_key(umo_key)
            if not umo_data:
                continue

            user_level = umo_data.get("active_level", umo_data.get("level", "1"))
            if user_level == "2":
                accessible = set(models_2 + models_1 + model_list)
            elif user_level == "1":
                accessible = set(models_1 + model_list)
            else:
                accessible = set(model_list)

            prefix_set = set(str_to_list(umo_data.get("prefixes", "")))
            changed = False
            for mn in all_affected:
                if mn in accessible and mn not in prefix_set:
                    prefix_set.add(mn)
                    changed = True
                elif mn not in accessible and mn in prefix_set:
                    prefix_set.discard(mn)
                    changed = True
                    if svc.sp.get(umo_key + ":current", "") == mn:
                        svc.storage.set_current_model_by_key(umo_key, StorageManager._default_model())
                        reset_users += 1

            if changed:
                umo_data["prefixes"] = _SM._list_to_str(sorted(prefix_set))
                svc.storage.set_umo_data_by_key(umo_key, umo_data)
                updated_users += 1

    for mn, from_levels, _ in moved:
        from_names = ", ".join(LEVEL_NAMES[l] for l in from_levels)
        log_msg(svc.wire, f"模型移动: {mn} {from_names} -> {LEVEL_NAMES[target_level]}")
    for mn in added_new:
        log_msg(svc.wire, f"模型新增: {mn} -> {LEVEL_NAMES[target_level]}")
    log_msg(svc.wire, f"热同步完成: 更新{updated_users}用户, 重置{reset_users}个当前模型")

    lines = []
    if moved:
        lines.append(f"📦 **已移动 {len(moved)} 个模型到 {LEVEL_NAMES[target_level]}:**")
        for mn, from_levels, _ in moved:
            from_names = ", ".join(LEVEL_NAMES[l] for l in from_levels)
            lines.append(f"  • `{mn}` ← {from_names}")
    if added_new:
        lvl_name = LEVEL_ID_PREFIX.get(target_level, "lv")
        lines.append(f"\n➕ **新增 {len(added_new)} 个模型:**")
        for i, mn in enumerate(added_new):
            idx = target_list.index(mn) + 1
            lines.append(f"  • `[{lvl_name}_{idx}]` {mn}")
    if skipped:
        lines.append(f"\n⏭ 已跳过(已在目标): {', '.join(skipped)}")
    lines.append(f"\n🔄 热同步: {updated_users} 用户前缀已更新")
    if reset_users:
        lines.append(f"⚠ {reset_users} 用户当前模型被重置为默认")
    yield event.plain_result("\n".join(lines))


async def cmd_delmodels(svc: Services, event, is_admin_fn):
    """批量删除模型或执行可达性测试。"""
    if not await is_admin_fn(event):
        yield event.plain_result("无权限")
        return

    parts = event.message_str.strip().split()
    cfg = svc.config_fn()

    # 无参数: API 连通性测试
    if len(parts) < 2:
        model_list = str_to_list(cfg.get("model_list", ""))
        models_1 = str_to_list(cfg.get("models_1", ""))
        models_2 = str_to_list(cfg.get("models_2", ""))

        all_models: dict[str, set[str]] = {}
        for m in model_list:
            all_models.setdefault(m, set()).add("公开")
        for m in models_1:
            all_models.setdefault(m, set()).add("Lv1")
        for m in models_2:
            all_models.setdefault(m, set()).add("Lv2")

        total = len(all_models)
        yield event.plain_result(f"🔍 正在对 {total} 个模型执行 API 连通性测试，请稍候...")

        context = svc.astrbot_context
        umo = event.unified_msg_origin
        from astrbot.core.provider.entities import ProviderType

        current_model_key = f"{SP_UMO_PREFIX}{umo}:current"
        original_model = svc.sp.get(current_model_key, "")

        results: list[tuple[str, str, bool, str]] = []
        for model_name, locations in sorted(all_models.items()):
            loc_tags = "+".join(sorted(locations))
            try:
                await context.provider_manager.set_provider(model_name, ProviderType.CHAT_COMPLETION, umo)
                provider_id = await context.get_current_chat_provider_id(umo)
                if not provider_id:
                    results.append((model_name, loc_tags, False, "无匹配的提供商"))
                    continue
                resp = await asyncio.wait_for(
                    context.llm_generate(chat_provider_id=provider_id, prompt="ping"),
                    timeout=15.0,
                )
                results.append((model_name, loc_tags, True, "OK"))
            except asyncio.TimeoutError:
                results.append((model_name, loc_tags, False, "超时(15s)"))
            except Exception as e:
                results.append((model_name, loc_tags, False, str(e)[:100]))

        if original_model and original_model != StorageManager._default_model():
            try:
                await context.provider_manager.set_provider(original_model, ProviderType.CHAT_COMPLETION, umo)
            except Exception:
                pass

        reachable = [r for r in results if r[2]]
        unreachable = [r for r in results if not r[2]]

        lines = ["📊 **模型 API 连通性测试报告**\n"]
        lines.append(f"✅ 可达: {len(reachable)} 个 | ❌ 不可达: {len(unreachable)} 个 | 总计: {total}\n")
        if reachable:
            lines.append("**可达模型:**")
            for model, loc, _, _ in reachable:
                lines.append(f"  ✅ `{model}` ({loc})")
        if unreachable:
            lines.append("\n**不可达模型:**")
            for model, loc, _, reason in unreachable:
                lines.append(f"  ❌ `{model}` ({loc}) — {reason}")
        lines.append("\n💡 使用 `/afdian_delmodels <模型名...>` 批量移除（空格分隔）")
        yield event.plain_result("\n".join(lines))
        return

    # 有参数: 批量删除
    model_names_del = [m.strip() for m in parts[1:] if m.strip()]
    if not model_names_del:
        yield event.plain_result("模型名不能为空")
        return

    model_list = str_to_list(cfg.get("model_list", ""))
    models_1 = str_to_list(cfg.get("models_1", ""))
    models_2 = str_to_list(cfg.get("models_2", ""))

    per_model: dict[str, list[str]] = {}
    deleted_set: set[str] = set()

    for mn in model_names_del:
        removed = []
        if mn in model_list:
            removed.append("公开")
        if mn in models_1:
            removed.append("Lv1")
        if mn in models_2:
            removed.append("Lv2")
        if removed:
            per_model[mn] = removed
            deleted_set.add(mn)

    model_list = [m for m in model_list if m not in deleted_set]
    models_1 = [m for m in models_1 if m not in deleted_set]
    models_2 = [m for m in models_2 if m not in deleted_set]

    if not per_model:
        yield event.plain_result(f"指定模型均不在任何层级中: {', '.join(model_names_del)}")
        return

    from .storage import StorageManager as _SM
    cfg["model_list"] = _SM._list_to_str(model_list)
    cfg["models_1"] = _SM._list_to_str(models_1)
    cfg["models_2"] = _SM._list_to_str(models_2)
    svc.config_manager.save_plugin_config(cfg)
    svc.plan_manager.sync_plan_mapping()

    user_updates = 0
    active_umos = svc.storage.get_active_umos()
    async with svc.storage._lock:
        for umo_key in active_umos:
            umo_data = svc.storage.get_umo_data_by_key(umo_key)
            if not umo_data:
                continue
            prefixes = str_to_list(umo_data.get("prefixes", ""))
            updated = False
            for mn in deleted_set:
                if mn in prefixes:
                    prefixes.remove(mn)
                    updated = True
                if svc.sp.get(umo_key + ":current", "") == mn:
                    svc.storage.set_current_model_by_key(umo_key, StorageManager._default_model())
            if updated:
                umo_data["prefixes"] = _SM._list_to_str(prefixes)
                svc.storage.set_umo_data_by_key(umo_key, umo_data)
                user_updates += 1

    for mn, removed in per_model.items():
        log_msg(svc.wire, f"模型删除: {mn} from {removed}")
    log_msg(svc.wire, f"热同步完成: config->plan_mapping->{user_updates}用户")

    not_found = [mn for mn in model_names_del if mn not in per_model]
    msg = f"✅ 已删除 {len(per_model)} 个模型:\n"
    for mn, removed in per_model.items():
        msg += f"  • `{mn}` ({', '.join(removed)})\n"
    msg += f"\n已热同步更新 {user_updates} 个活跃用户"
    if not_found:
        msg += f"\n未找到(已跳过): {', '.join(not_found)}"
    yield event.plain_result(msg)
