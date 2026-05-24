# v3.0 全面重构 — 开发记录

**日期**: 2026-05-24
**分支**: main (commit 5bf566d)

## 问题诊断（用户审查反馈）

| 问题 | 严重程度 | 根因 |
|------|---------|------|
| 单文件臃肿 | 🔴 | commands_admin.py 670行，main.py __init__ 30行 |
| 重复签名逻辑 | 🔴 | afdian_api 内联 import re/time，__import__("time") |
| 打洞注入 | 🟡 | UserCommands 构造函数传 7 个参数 |
| 裸 except:pass | 🟡 | 异常被静默吞掉 |
| 持久化混乱 | 🟡 | processed_orders.json 与 sp 双重管理 |
| 长函数 | 🟡 | _cron_daily 40+ 行，单函数超 50 行 |
| 缺少类型注解 | 🟢 | 全项目无 typing |

## 重构方案

### 1. 提取 utils.py（统一工具层）

```
utils.py
├── AfdianSigner        — 爱发电 API 签名（消除 afdian_api 重复）
├── list_to_str / str_to_list — 序列化（消除 StorageManager/PlanManager 重复）
├── model_names_from_config    — 配置解析
├── atomic_write / atomic_write_json / safe_read_json — 原子 I/O
└── log_msg / PLUGIN_LOG_PREFIX — 统一日志
```

### 2. 引入 services.py（依赖容器）

```python
class Services:  # 替代 7 参数构造函数
    api_getter: ApiGetter
    config_fn: ConfigFn
    config_manager: ConfigManager
    storage: StorageManager
    plan_manager: PlanManager
    user_manager: UserManager
    wire: LogFn
```

所有命令函数签名从 `(api, config, storage, plan, user, wire, event)` → `(svc: Services, event)`

### 3. 拆分命令文件

| 文件 | 行数 | 职责 |
|------|------|------|
| `commands_admin.py` | 670→240 | reset/reset_all/query/getconfig/setconfig/migrateconfig |
| `commands_model.py` | 新增 | addmodels/delmodels |
| `commands_plan.py` | 新增 | addplan/delplan |
| `commands_user.py` | 重构 | help/bind/models/switch/status |

### 4. 异常处理规范化

- 消除所有 `except Exception: pass`
- 精确捕获 `json.JSONDecodeError`、`OSError`、`FileNotFoundError`、`ValueError`
- `save_processed_orders_nolock` 内部方法移除，统一通过 `with self._lock` 保护

### 5. storage.py / plan_manager.py 委托 utils

- `_list_to_str` / `_str_to_list` → 委托 `utils.list_to_str` / `utils.str_to_list`
- `PlanManager._parse_models` → 委托 `utils.model_names_from_config`
- `_save_processed_orders` → 委托 `utils.atomic_write_json`
- `_load_processed_orders` → 委托 `utils.safe_read_json`

## 改动统计

| 文件 | 改动 | 说明 |
|------|------|------|
| `utils.py` | 新增 97 行 | 签名/序列化/原子I/O/日志 |
| `services.py` | 新增 46 行 | 依赖容器 |
| `commands_model.py` | 新增 250 行 | 模型管理命令 |
| `commands_plan.py` | 新增 56 行 | 方案管理命令 |
| `commands_admin.py` | 670→240 行 | 精简为核心管理 |
| `commands_user.py` | 重构 | 使用 Services 容器 |
| `main.py` | __init__ 5 行 | Services 容器 + 懒加载命令 import |
| `storage.py` | 精简 | 委托 utils |
| `plan_manager.py` | 精简 | 委托 utils + 注入 storage |
| `afdian_api.py` | 消除重复 | 使用 AfdianSigner |
| `cron_tasks.py` | 重构 | 使用 Services 容器 |
| `user_manager.py` | 重构 | 使用 utils 序列化 |

## 架构对比

```
重构前:
main.py.__init__ ──→ ConfigManager(wire)
                 ├──→ StorageManager(wire)  ← 各自独立
                 ├──→ PlanManager(config, wire)  ← 自建 StorageManager!
                 ├──→ UserManager(storage, plan, wire)
                 ├──→ UserCommands(api, config, cfg_mgr, storage, plan, user, wire)  ← 7 参数!
                 ├──→ AdminCommands(api, config, cfg_mgr, storage, plan, wire)  ← 6 参数!
                 └──→ CronTasks(api, storage, plan, wire)  ← 4 参数!

重构后:
main.py.__init__ ──→ _build_services()
                      ├── ConfigManager(DATA_DIR, wire)
                      ├── StorageManager(DATA_DIR, wire)  ← 共享
                      ├── PlanManager(config, storage, wire)
                      ├── UserManager(storage, plan, wire)
                      └── Services(...)  ← 一个容器

命令: cmd_xxx(svc: Services, event)  ← 1 参数!
```

## 验证

- [x] 全部 13 文件 py_compile 通过
- [x] Ni-ShuWu 母仓库推送成功
- [x] kelai141 fork 同步成功
- [x] metadata.yaml repo 链接正确
