# 持久化存储全面修复 — 开发记录

**日期**: 2026-05-23
**分支**: main

## 问题概述

四项持久化存储缺陷需修复：
1. **存储位置不合规** — data/ 放在插件自身目录，更新/重装插件会丢数据
2. **_dump_state() 过度调用** — bind_user 一次操作触发 3 次全量快照写盘
3. **并发写入不安全** — tmp→rename 无锁保护，命令与定时任务可能数据竞争
4. **重载后 models 列表丢失** — WebUI 配置被 plugin_config.json 旧数据覆盖

## 根因分析

### Bug 4 根因（重载后 models 丢失）

`_config()` 旧逻辑：
```
plugin_config.json 存在 → 直接返回（忽略 WebUI _star_config）
```

当管理员通过 WebUI 修改 model_list 后，数据写入 AstrBot 框架配置（`_star_config`），但 `plugin_config.json` 仍保留旧值。重载插件后 `_config()` 首选 `plugin_config.json`，WebUI 修改被丢弃。

### Bug 1 根因（存储位置）

`DATA_DIR = os.path.join(PLUGIN_DIR, "data")` — 插件目录在 `data/plugins/<name>/`，重装时可能被清空。AstrBot 规范要求数据放入框架级 `data/plugin_data/<name>/`。

### Bug 2 根因（过度调用）

每个 StorageManager set 方法都独立调用 `_dump_state()` 全量快照。`bind_user` 调用链：`set_umo_data` → `register_umo` → `set_user_mapping`，各触发一次文件写入。

### Bug 3 根因（并发）

`_dump_state()` 和 `save_plugin_config()` 无锁，tmp→rename 非原子。用户命令和 `_cron_daily`/`_cron_poll` 可能同时写同一文件。

## 修复方案

### 1. 存储位置迁移 (`main.py` + `storage.py` + `config.py`)

- `main.py`: 引入 `get_astrbot_data_path()`，计算路径 `data/plugin_data/astrbot_plugin_afdian_model/`
- `StorageManager` 和 `ConfigManager`: 改为接收外部 `data_dir` 参数，不再自己计算路径
- 旧 `PLUGIN_DIR/data/` 文件需手动迁移（或首次启动时自动创建新位置）

### 2. 配置合并策略 (`main.py:_config()`)

新逻辑：
```
_star_config (WebUI) 优先 + plugin_config.json 补充缺失 key
        ↓
合并结果写回 plugin_config.json（保持同步）
```

- 每次读取都做合并，确保 WebUI 修改不被旧文件覆盖
- plugin_config.json 退化为崩溃恢复的缓存副本

### 3. Batch commit (`storage.py` + `user_manager.py` + `commands_admin.py` + `cron_tasks.py`)

新增：
```python
def begin_batch(self):   # _batch_depth += 1
def end_batch(self):     # _batch_depth -= 1，归零时调用 _dump_state()
```

所有 set 方法改为 `if not self._batch_depth: self._dump_state()`

**使用 batch 的场景：**
- `user_manager.py:bind_user()` — 3 次写合并为 1 次
- `commands_admin.py:cmd_addmodels()` — 循环内写合并
- `commands_admin.py:cmd_delmodels()` — 循环内写合并
- `commands_admin.py:cmd_reset()` — 降级分支写合并
- `cron_tasks.py:_downgrade_to_lv0()` — 2 次写合并

### 4. 并发安全 (`storage.py` + `config.py`)

- `StorageManager`: 新增 `threading.Lock`，保护 `_dump_state()`、`_save_processed_orders()`、`mark_order_processed()`、`unmark_order_processed()`、`clear_orders()`
- `ConfigManager`: 新增 `threading.Lock`，保护 `save_plugin_config()`
- 单线程 asyncio 环境 + `threading.Lock` 足以防止协程交错（无 `await` 在锁内）

## 改动文件

| 文件 | 改动 | 说明 |
|------|------|------|
| `main.py` | +40/-35 | 框架 data 路径；`_config()` 合并策略；提取 `_try_migrate_astrbot_config` |
| `storage.py` | +40/-18 | data_dir 注入；threading.Lock；batch_depth；_nolock 内部方法 |
| `config.py` | +10/-10 | data_dir 注入；threading.Lock |
| `user_manager.py` | +4/-0 | bind_user 两处 batch |
| `commands_admin.py` | +4/-0 | addmodels/delmodels/reset 三处 batch |
| `cron_tasks.py` | +2/-0 | downgrade_to_lv0 batch |

## 验证

- [x] 全部 9 文件 `py_compile` 通过
- [x] batch depth 正确：begin/end 配对
- [x] 锁正确：无 await 在锁内
- [x] _config 合并逻辑：star_cfg 优先但不覆盖 file_cfg 独有 key
