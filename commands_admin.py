import asyncio
import os
from astrbot.core import sp
from .config import ConfigManager
from .storage import StorageManager, SP_UMO_PREFIX
from .plan_manager import PlanManager

# 模型编号映射
LEVEL_ID_PREFIX = {"0": "zero", "1": "one", "2": "two"}



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
        plan_id = found.get("plan_id", "")
        plan_mapping = self._plan_manager.get_plan_mapping()

        # 确定订单对应的方案信息
        plan_info = plan_mapping.get(plan_id)
        if not plan_info:
            plan_info = self._plan_manager.verify_and_get_plan(plan_id)
        if not plan_info:
            self._wire(f"[AfdianModel] 重置: 订单{order_no}的方案{plan_id}未找到映射，仅清理绑定")
            plan_days = 0
            plan_level = "1"
        else:
            plan_days = plan_info.get("days", 0)
            # 从配置直接判定等级，避免依赖 get_plan_mapping 中缺失的 level 字段
            cfg = self._config_fn()
            if plan_id == cfg.get("plan_id_2", "").strip():
                plan_level = "2"
            elif plan_id == cfg.get("plan_id_1", "").strip():
                plan_level = "1"
            else:
                plan_level = plan_info.get("level", "1")

        umo_key = self._storage.get_user_mapping(user_id)
        deducted_msg = ""
        if umo_key:
            umo_data = sp.get(umo_key, {})
            if umo_data:
                umo_data = dict(umo_data)
                # 迁移旧数据
                umo_data = StorageManager.migrate_umo_data(umo_data, self._wire)

                # 从 used_orders 中移除此订单
                used_orders = umo_data.get("used_orders", [])
                if order_no in used_orders:
                    used_orders.remove(order_no)
                    umo_data["used_orders"] = used_orders
                    self._wire(f"[AfdianModel] 重置: 从用户数据移除订单{order_no}")

                # 扣减对应等级的天数
                if plan_days > 0:
                    if plan_level == "2":
                        old_l2 = umo_data.get("l2_days", 0)
                        umo_data["l2_days"] = max(0, old_l2 - plan_days)
                        deducted = old_l2 - umo_data["l2_days"]
                        deducted_msg = f"，已扣减 Lv2 {deducted} 天"
                        # 如果 Lv2 归零，降级到 Lv1
                        if umo_data["l2_days"] <= 0 and umo_data.get("l1_days", 0) > 0:
                            umo_data["active_level"] = "1"
                            deducted_msg += "，已切换至 Lv1"
                    else:
                        old_l1 = umo_data.get("l1_days", 0)
                        umo_data["l1_days"] = max(0, old_l1 - plan_days)
                        deducted = old_l1 - umo_data["l1_days"]
                        deducted_msg = f"，已扣减 Lv1 {deducted} 天"

                umo_data["remaining_days"] = umo_data.get("l1_days", 0) + umo_data.get("l2_days", 0)

                # 余额归零 → 降为 Lv0
                if umo_data["remaining_days"] <= 0 or not used_orders:
                    umo_data["active_level"] = "0"
                    umo_data["level"] = "0"
                    umo_data["l1_days"] = 0
                    umo_data["l2_days"] = 0
                    umo_data["remaining_days"] = 0
                    self._storage.begin_batch()
                    self._storage.set_umo_data_by_key(umo_key, umo_data)
                    self._storage.unregister_umo(umo_key)
                    self._storage.end_batch()
                    deducted_msg += "，余额归零已降为 Lv0"
                    self._wire(f"[AfdianModel] 重置: 用户{user_id}余额归零，降为Lv0")
                else:
                    self._storage.set_umo_data_by_key(umo_key, umo_data)

        self._storage.unmark_order_processed(order_no)

        self._wire(f"[AfdianModel] 订单重置成功: order={order_no} user={user_id}{deducted_msg}")
        yield event.plain_result(
            f"订单 {order_no} 已重置{deducted_msg}，可以重新绑定"
        )

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
                "• 所有已绑定的订单记录\n"
                "• 所有用户的赞助信息\n"
                "• 所有活跃绑定与模型映射\n"
                "• 持久化文件（persistence.json）\n\n"
                "**不会清除：**\n"
                "• 插件配置文件（模型列表、方案ID等）\n\n"
                "如需执行，请输入：\n"
                "`/afdian_reset_all YES`"
            )
            return

        stats = self._storage.full_reset()

        self._wire(f"[AfdianModel] 一键重置完成: {stats}")
        yield event.plain_result(
            f"✅ **一键重置完成！**\n\n"
            f"已清除：\n"
            f"• {stats['orders']} 条订单记录\n"
            f"• {stats['umo_data']} 条用户数据\n"
            f"• {stats['user_mappings']} 个用户映射\n"
            f"• {stats['active_umos']} 个活跃绑定\n"
            f"• persistence.json 持久化文件\n\n"
            f"插件配置已保留，可正常使用。"
        )

    async def cmd_addmodels(self, event, is_admin_fn):
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

        cfg = self._config_fn()

        # 读取所有等级列表
        model_list = self._storage._str_to_list(cfg.get("model_list", ""))
        models_1 = self._storage._str_to_list(cfg.get("models_1", ""))
        models_2 = self._storage._str_to_list(cfg.get("models_2", ""))

        LEVEL_LISTS = {"0": model_list, "1": models_1, "2": models_2}
        LEVEL_NAMES = {"0": "公开(Lv0)", "1": "方案1(Lv1)", "2": "方案2(Lv2)"}

        target_list = LEVEL_LISTS[target_level]

        # 分类：移动(从其他等级迁入) / 新增 / 已在目标
        moved = []      # (model_name, [from_levels], to_level)
        added_new = []  # 全新模型
        skipped = []    # 已在目标等级

        for mn in model_names:
            if mn in target_list:
                skipped.append(mn)
                continue

            from_levels = []
            for lvl, lst in LEVEL_LISTS.items():
                if lvl != target_level and mn in lst:
                    from_levels.append(lvl)
                    lst.remove(mn)

            target_list.append(mn)
            if from_levels:
                moved.append((mn, from_levels, target_level))
            else:
                added_new.append(mn)

        if not moved and not added_new:
            yield event.plain_result(f"所有模型已在{LEVEL_NAMES[target_level]}中: {', '.join(model_names)}")
            return

        # 保存配置
        cfg["model_list"] = self._storage._list_to_str(model_list)
        cfg["models_1"] = self._storage._list_to_str(models_1)
        cfg["models_2"] = self._storage._list_to_str(models_2)
        self._config_manager.save_plugin_config(cfg)
        self._plan_manager.sync_plan_mapping()

        # 热同步：遍历所有活跃用户，按兼容规则同步前缀
        # Lv2 可见: models_2 + models_1 + model_list
        # Lv1 可见: models_1 + model_list
        # Lv0 可见: model_list
        all_moved_models = {mn for mn, _, _ in moved}
        all_affected = all_moved_models | set(added_new)

        updated_users = 0
        reset_users = 0
        active_umos = self._storage.get_active_umos()

        self._storage.begin_batch()
        for umo_key in active_umos:
            umo_data = sp.get(umo_key, {})
            if not umo_data:
                continue

            user_plan_id = umo_data.get("plan_id", "")
            user_level = umo_data.get("active_level", umo_data.get("level", "1"))

            # 判断用户可访问的等级范围
            if user_level == "2":
                accessible = set(models_2 + models_1 + model_list)
            elif user_level == "1":
                accessible = set(models_1 + model_list)
            else:
                accessible = set(model_list)

            prefixes = self._storage._str_to_list(umo_data.get("prefixes", ""))
            prefix_set = set(prefixes)
            changed = False

            for mn in all_affected:
                if mn in accessible and mn not in prefix_set:
                    prefix_set.add(mn)
                    changed = True
                elif mn not in accessible and mn in prefix_set:
                    prefix_set.discard(mn)
                    changed = True
                    # 如果当前模型被移除，重置为默认
                    if sp.get(umo_key + ":current", "") == mn:
                        self._storage.set_current_model_by_key(umo_key, "默认模型")
                        reset_users += 1

            if changed:
                umo_data["prefixes"] = self._storage._list_to_str(sorted(prefix_set))
                self._storage.set_umo_data_by_key(umo_key, umo_data)
                updated_users += 1
        self._storage.end_batch()

        # 日志
        for mn, from_levels, _ in moved:
            from_names = ", ".join(LEVEL_NAMES[l] for l in from_levels)
            self._wire(f"[AfdianModel] 模型移动: {mn} {from_names} → {LEVEL_NAMES[target_level]}")
        for mn in added_new:
            self._wire(f"[AfdianModel] 模型新增: {mn} → {LEVEL_NAMES[target_level]}")
        self._wire(f"[AfdianModel] 热同步完成: 更新{updated_users}用户, 重置{reset_users}个当前模型")

        # 构建回复消息
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

    async def cmd_delmodels(self, event, is_admin_fn):
        if not await is_admin_fn(event):
            yield event.plain_result("无权限")
            return

        parts = event.message_str.strip().split()
        cfg = self._config_fn()

        # 无参数: API连通性测试（真实调用模型API验证）
        if len(parts) < 2:
            model_list = self._storage._str_to_list(cfg.get("model_list", ""))
            models_1 = self._storage._str_to_list(cfg.get("models_1", ""))
            models_2 = self._storage._str_to_list(cfg.get("models_2", ""))

            all_models = {}
            for m in model_list:
                all_models[m] = all_models.get(m, set()) | {"公开"}
            for m in models_1:
                all_models[m] = all_models.get(m, set()) | {"Lv1"}
            for m in models_2:
                all_models[m] = all_models.get(m, set()) | {"Lv2"}

            total = len(all_models)
            yield event.plain_result(f"🔍 正在对 {total} 个模型执行 API 连通性测试，请稍候...")

            context = event._message_context
            umo = event.unified_msg_origin
            from astrbot.core.provider.entities import ProviderType

            # 保存管理员当前模型，测试结束后恢复
            current_model_key = f"{SP_UMO_PREFIX}{umo}:current"
            original_model = sp.get(current_model_key, "")

            results = []
            for model_name, locations in sorted(all_models.items()):
                loc_tags = "+".join(sorted(locations))
                try:
                    await context.provider_manager.set_provider(
                        model_name, ProviderType.CHAT_COMPLETION, umo
                    )
                    provider_id = await context.get_current_chat_provider_id(umo)
                    if not provider_id:
                        results.append((model_name, loc_tags, False, "无匹配的提供商"))
                        continue

                    resp = await asyncio.wait_for(
                        context.llm_generate(
                            chat_provider_id=provider_id,
                            prompt="ping",
                        ),
                        timeout=15.0
                    )
                    results.append((model_name, loc_tags, True, "OK"))
                except asyncio.TimeoutError:
                    results.append((model_name, loc_tags, False, "超时(15s)"))
                except Exception as e:
                    err_msg = str(e)[:100]
                    results.append((model_name, loc_tags, False, err_msg))

            # 恢复管理员原来的模型
            if original_model and original_model != "默认模型":
                try:
                    await context.provider_manager.set_provider(
                        original_model, ProviderType.CHAT_COMPLETION, umo
                    )
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

        # 有参数: 批量删除模型
        model_names = [m.strip() for m in parts[1:] if m.strip()]
        if not model_names:
            yield event.plain_result("模型名不能为空")
            return

        # 预处理各级别列表（只读一次配置）
        model_list = self._storage._str_to_list(cfg.get("model_list", ""))
        models_1 = self._storage._str_to_list(cfg.get("models_1", ""))
        models_2 = self._storage._str_to_list(cfg.get("models_2", ""))

        per_model = {}  # model_name → removed_from list
        deleted_set = set()

        for mn in model_names:
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

        # 从列表中去重移除
        model_list = [m for m in model_list if m not in deleted_set]
        models_1 = [m for m in models_1 if m not in deleted_set]
        models_2 = [m for m in models_2 if m not in deleted_set]

        if not per_model:
            yield event.plain_result(f"指定模型均不在任何层级中: {', '.join(model_names)}")
            return

        cfg["model_list"] = self._storage._list_to_str(model_list)
        cfg["models_1"] = self._storage._list_to_str(models_1)
        cfg["models_2"] = self._storage._list_to_str(models_2)

        # 持久化配置
        self._config_manager.save_plugin_config(cfg)

        # 同步方案映射
        self._plan_manager.sync_plan_mapping()

        # 热同步: 更新所有受影响活跃用户
        from astrbot.core import sp
        user_updates = 0
        active_umos = self._storage.get_active_umos()
        self._storage.begin_batch()
        for umo_key in active_umos:
            umo_data = sp.get(umo_key, {})
            if not umo_data:
                continue
            prefixes = self._storage._str_to_list(umo_data.get("prefixes", ""))
            updated = False
            for mn in deleted_set:
                if mn in prefixes:
                    prefixes.remove(mn)
                    updated = True
                current_model_key = umo_key + ":current"
                if sp.get(current_model_key, "") == mn:
                    self._storage.set_current_model_by_key(umo_key, "默认模型")
            if updated:
                umo_data["prefixes"] = self._storage._list_to_str(prefixes)
                self._storage.set_umo_data_by_key(umo_key, umo_data)
                user_updates += 1
        self._storage.end_batch()

        for mn, removed in per_model.items():
            self._wire(f"[AfdianModel] 模型删除: {mn} from {removed}")
        self._wire(f"[AfdianModel] 热同步完成: config→plan_mapping→{user_updates}用户")

        not_found = [mn for mn in model_names if mn not in per_model]
        msg = f"✅ 已删除 {len(per_model)} 个模型:\n"
        for mn, removed in per_model.items():
            msg += f"  • `{mn}` ({', '.join(removed)})\n"
        msg += f"\n已热同步更新 {user_updates} 个活跃用户"
        if not_found:
            msg += f"\n未找到(已跳过): {', '.join(not_found)}"
        yield event.plain_result(msg)

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

        mapping = self._storage.get_plan_mapping()
        mapping[plan_id] = {"days": days, "prefixes": self._storage._list_to_str(prefixes)}
        self._storage.set_plan_mapping(mapping)
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
        mapping = self._storage.get_plan_mapping()
        auto_key = f"_auto_{plan_id}"
        deleted = False
        if plan_id in mapping:
            del mapping[plan_id]
            deleted = True
        if auto_key in mapping:
            del mapping[auto_key]
            deleted = True
        if deleted:
            self._storage.set_plan_mapping(mapping)
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
                plan_info = "\n⚠ 方案未配置"
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
