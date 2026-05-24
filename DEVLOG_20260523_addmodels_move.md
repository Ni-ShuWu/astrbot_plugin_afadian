# addmodels 模型等级迁移功能 — 开发记录

**日期**: 2026-05-23
**分支**: main (commit 4c9ab25)

## 需求

`/afdian_addmodels` 增加模型等级迁移能力：若模型已存在于其他等级，自动从旧等级删除并移动到目标等级。

## 实现

### 等级兼容规则（已有，复用）
- Lv2 可见：models_2 + models_1 + model_list
- Lv1 可见：models_1 + model_list
- Lv0 可见：model_list

### 处理流程

```
输入: /afdian_addmodels 2 gpt-4o
  │
  ├─ 1. 读取三个等级列表
  ├─ 2. 检查 gpt-4o 位置
  │     ├─ 在 Lv0 → from_levels=["0"]
  │     ├─ 在 Lv1 → from_levels=["1"]
  │     └─ 在 Lv2 → skipped（已在目标）
  ├─ 3. 从所有旧等级删除，添加到目标等级
  ├─ 4. 保存配置 + sync_plan_mapping
  ├─ 5. 热同步：遍历所有活跃用户
  │     ├─ 按用户等级计算 accessible 集合
  │     ├─ 模型在 accessible 且不在 prefixes → 添加
  │     ├─ 模型不在 accessible 但在 prefixes → 移除
  │     └─ 当前模型被移除 → 重置为默认模型
  └─ 6. 回复：移动/新增/跳过统计 + 受影响用户数
```

### 效果示例

| 操作 | Lv0 用户 | Lv1 用户 | Lv2 用户 |
|------|---------|---------|---------|
| `gpt-4o` Lv0→Lv2 | ❌ 失去 | ❌ 失去 | ✅ 获得 |
| `gpt-4o` Lv2→Lv0 | ✅ 获得 | ✅ 获得 | ✅ 保持 |
| `gpt-4o` Lv0→Lv1 | ❌ 失去 | ✅ 获得 | ✅ 保持 |
| `gpt-4o` Lv1→Lv2 | — | ❌ 失去 | ✅ 保持 |

### 回复格式

```
📦 已移动 1 个模型到 方案2(Lv2):
  • gpt-4o ← 公开(Lv0)

➕ 新增 2 个模型:
  • [zero_3] claude-3.5
  • [zero_4] gemini-2.5

⏭ 已跳过(已在目标): already_there_model
🔄 热同步: 5 用户前缀已更新
⚠ 2 用户当前模型被重置为默认
```

## 改动文件

| 文件 | 改动 | 说明 |
|------|------|------|
| `commands_admin.py` | +104/-72 | 重写 cmd_addmodels：统一处理三个等级列表、等级迁移、兼容规则热同步 |

## 验证

- [x] py_compile 通过
- [x] 全新模型：行为不变（added_new 路径）
- [x] 已在目标：skipped（幂等）
- [x] 跨等级移动：from_levels 删除 + target 添加
- [x] 热同步：按兼容规则正确 add/remove 前缀
- [x] 降级用户当前模型重置
