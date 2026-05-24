# 重载重复累加修复 + reset_all 全量清除 — 开发记录

**日期**: 2026-05-14
**分支**: fix/reload-daydouble-resetall → PR #18

## 问题

### Bug 1: 剩余天数重复累加（未修好）
上一轮修复（PlanManager 注入 StorageManager）并非根因。真正根因是：
- `cmd_reset_all` 删除 `processed_orders.json` 但**保留了 `persistence.json`**
- 插件重载 → `restore_state()` 从 `persistence.json` 恢复用户数据到 sp
- `_cron_poll` 启动 → `is_order_processed()` 读空文件 → 返回 False
- **所有已处理订单被重新累加天数**

### Bug 2: reset all 指令无法正常清除用户数据
- `cmd_reset_all` 只清了 `active_umos` 和部分 `umo_data`，**未清除** `user_mappings`、`user_index`、`plan_mapping`、`persistence.json`
- 导致"持久化文件数据残留 + sp 状态残留"，重载后旧数据复活且被重复处理

### 附带修复: _cron_poll 只查第 1 页
原 `_cron_poll` 固定 `page=1`，多页订单被忽略，导致部分用户无法自动绑定。

## 修复

### 1. `_process_single_order` 去重逻辑重构
**原序**（错误）：
1. `is_order_processed(file)` — 依赖文件
2. `mark_order_processed(file)` — 先标记
3. `used_orders in umo_data` 检查 — 后检查（太晚）

**新序**（正确）：
1. **`used_orders` 检查优先** — 从 sp 内存判断，不依赖文件
2. 文件级去重兜底 — `is_order_processed`
3. `mark_order_processed` — 最后标记

**效果**：即使 `processed_orders.json` 丢失，umo_data 中的 `used_orders` 仍能阻止重复累加。

### 2. `_cron_poll` 多页轮询
```python
while True:
    resp = await api.query_order(page=pg)
    orders = data.get("list", [])
    for order in orders:
        await self._process_single_order(order)
    total_pages = data.get("total_page", 1)
    if pg >= total_pages: break
    pg += 1
```

### 3. `StorageManager.full_reset()` — 全量清除
新增方法，完全清除：
- 订单记录（processed_orders.json）
- 所有 umo 数据（SP_UMO_PREFIX:*）
- 用户映射（SP_BY_AFDIAN:*）
- 活跃绑定列表（SP_ACTIVE_UMOS）
- 方案映射（SP_PLAN_MAPPING）
- 用户索引（SP_USER_INDEX）
- 持久化文件（persistence.json）

**保留**：插件配置文件（模型列表、方案 ID、API 密钥等）

### 4. `cmd_reset_all` 重写
改用 `full_reset()` 一站式清除，输出详细统计：
- N 条订单记录
- N 条用户数据
- N 个用户映射
- N 个活跃绑定

## 改动文件

| 文件 | 改动 | 说明 |
|------|------|------|
| `cron_tasks.py` | ~60 行重构 | _cron_poll 多页 + _process_single_order 去重前置 |
| `storage.py` | +35 行 | 新增 full_reset() 方法 |
| `commands_admin.py` | ~20 行改写 | cmd_reset_all 使用 full_reset() |

## 验证

- [x] 全部 7 文件 py_compile 通过
- [x] _process_single_order used_orders 检查在 mark 之前
- [x] _cron_poll 遍历所有页
- [x] full_reset() 覆盖所有 sp 键和持久化文件
