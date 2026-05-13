# 插件重载Bug修复 + 余额重置/Lv0降级 — 开发记录

**日期**: 2026-05-13
**分支**: main (直接提交)

## 问题概述

用户报告两个严重bug：
1. **插件重载后模型列表丢失** — 用户可用的模型在插件重载后消失
2. **插件重载后剩余天数重复累加** — 重载后用户的天数被额外加上一次

同时提出三项增强需求：
3. **reset指令需同时重置用户余额** — 重置订单后应扣减对应用户的赞助天数
4. **余额耗尽自动降为Lv0** — 不删除用户数据，保留绑定关系，仅清除付费权限
5. **代码卫生检查**

## 根因分析

### Bug 1 & 2 根因：PlanManager 独立 StorageManager 实例

**问题代码** (`plan_manager.py:9`)：
```python
class PlanManager:
    def __init__(self, config_fn, wire_fn=None):
        self._storage = StorageManager(wire_fn)  # ← 创建了独立的 StorageManager！
```

知识库文档明确指出："所有模块应通过**同一个 StorageManager 实例**操作持久化数据，避免出现多个 StorageManager 各自维持一份 `_dump_state()` 快照，造成数据覆盖或不一致。"

**攻击链**：
1. `main.py` 创建主 `StorageManager` → `restore_state()` 从文件恢复 sp 数据
2. `PlanManager.__init__` 创建**第二个** `StorageManager`
3. `sync_plan_mapping()` 通过 PlanManager 的 StorageManager 写入 `plan_mapping`，触发 `_dump_state()`
4. 两个 StorageManager 各自管理 `_processed_orders` 集合和序列化时机，产生数据竞争
5. 重载后 sp 状态可能被部分覆盖或顺序异常

### Bug 3：_cleanup_expired 完全删除用户数据

原逻辑在余额耗尽时调用 `_cleanup_expired` → 删除 umo_data + 移除 user_mapping + 清除 :current 键。用户完全消失，无法回看历史记录，也无法重新绑定时有积累基础。

### Bug 4：Lv2→Lv1 切换当天消耗 Lv1 天数

原代码在 Lv2 耗尽切换到 Lv1 的当天，除了设置 `active_level="1"` 外还执行了 `l1_days -= 1`，导致 Lv1 在切换首日就被扣了一天。

## 修复方案

### 1. PlanManager 注入共享 StorageManager (`plan_manager.py` + `main.py`)

```python
# plan_manager.py: 接收外部 storage 而非创建新的
class PlanManager:
    def __init__(self, config_fn, storage, wire_fn=None):
        self._storage = storage  # 使用注入的实例

# main.py: 传入主 StorageManager
self._plan_manager = PlanManager(self._config, self._storage, self._wire)
```

### 2. _cleanup_expired → _downgrade_to_lv0 (`cron_tasks.py`)

不再删除用户数据，而是：
- 设置 `active_level="0"`, `level="0"`
- 清空 `l1_days=0`, `l2_days=0`, `remaining_days=0`
- 保留 `umo_data` 和 `user_mapping`
- 从 `active_umos` 中移除（不再参与每日扣减）
- 恢复默认 provider

### 3. 修复 Lv2→Lv1 切换逻辑 (`cron_tasks.py`)

重写 `_cron_daily` 消费逻辑：
```python
if active_level == "2":
    l2_days -= 1          # 仅扣 Lv2
    if l2_days <= 0:
        if l1_days > 0:
            data["active_level"] = "1"  # 切换但不扣 Lv1
            l2_days = 0
        else:
            self._downgrade_to_lv0(key)  # 全部耗尽
elif active_level == "1":
    l1_days -= 1          # 仅扣 Lv1
    if l1_days <= 0:
        self._downgrade_to_lv0(key)
```

### 4. cmd_reset 增加余额扣减 (`commands_admin.py`)

重置订单时：
- 通过 `plan_id` 从配置/映射中查找方案等级和天数
- 从用户的 `l1_days` 或 `l2_days` 中扣减对应天数
- 从 `used_orders` 中移除此订单号
- 如余额归零 → 降为 Lv0

### 5. Lv0 降级用户可正常使用公开模型

修复三处 Lv0 支持：
- **`_get_all_available_models()`**: 添加 `else` 分支，Lv0 用户显示公开模型列表
- **`has_model_permission()`**: 添加 `user_level == "0"` 分支，允许使用 Lv0 公开模型
- **`cmd_switch()`**: Lv0 降级用户在群聊中拦截，仅允许私聊切换

### 6. 消除迁移代码重复 (`storage.py`)

将 `UserManager._migrate_umo_data` 和 `CronTasks._migrate_data` 的重复逻辑提取为 `StorageManager.migrate_umo_data()` 静态方法，单一来源。

## 改动文件

| 文件 | 改动 | 说明 |
|------|------|------|
| `plan_manager.py` | -3/+3 | 移除独立 StorageManager，接收注入实例 |
| `main.py` | -1/+1 | 传入主 StorageManager 到 PlanManager |
| `cron_tasks.py` | -42/+38 | 重写 _cron_daily；_cleanup_expired→_downgrade_to_lv0；委托迁移方法 |
| `user_manager.py` | -28/+4 | _migrate_umo_data 委托到 StorageManager；新增 Lv0 权限检查 |
| `storage.py` | +26 | 新增 migrate_umo_data 静态方法 |
| `commands_admin.py` | +62/-18 | cmd_reset 增加余额扣减与等级判定 |
| `commands_user.py` | +15/-1 | Lv0 用户模型列表及群聊切换限制 |

## 验证

- [x] 全部 7 个文件 `py_compile` 通过
- [x] 语法正确，无新增 lint 问题
- [x] 消除迁移方法代码重复
- [x] Lv0 降级用户可正常使用公开模型
