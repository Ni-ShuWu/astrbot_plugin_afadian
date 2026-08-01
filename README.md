# 爱发电模型订阅 (astrbot_plugin_afadian)

基于爱发电赞助的 LLM 模型自助切换插件。用户通过爱发电赞助后获得模型选择权，可在私聊中自助切换 LLM 模型；群聊中仅群主/群管可为群切换模型。权限到期自动收回。

## 安装

1. 在 AstrBot 管理面板上传插件 zip 包（或从插件市场安装）
2. 重启 AstrBot 或在插件管理中启用本插件
3. 依赖自动安装（`aiohttp`）

## 配置

在 AstrBot WebUI 插件管理 → 本插件 → 配置：

| 配置项 | 说明 |
| --- | --- |
| `afdian_user_id` | 爱发电用户 ID，在 [爱发电开发者后台](https://afdian.net/dashboard/dev) 获取 |
| `afdian_token` | 爱发电 API Token |
| `model_list` | 公开（Lv0）模型名称列表，一行/逗号分隔一个 |
| `models_1` / `models_2` | Lv1/Lv2 赞助方案可用模型列表 |
| `plan_id_1` / `plan_id_2` | 一级/二级赞助方案的 plan_id |
| `days_1` / `days_2` | 一级/二级方案每次赞助增加的天数 |
| `group_admins` | 静态群管理员列表 `{群号: [QQ]}`（可选，平台群角色信息不可用时兜底） |
| `poll_interval_hours` | 爱发电订单轮询间隔（小时），默认 1 小时 |

## 命令一览

### 👤 用户指令（仅私聊）

| 命令 | 说明 |
| --- | --- |
| `/afdian_bind <订单号>` | 绑定爱发电订单号，获得模型使用权 |
| `/afdian_models` | 查看当前可用的模型列表 |
| `/afdian_switch <模型名>` | 切换当前使用的模型 |
| `/afdian_status` | 查看赞助权限状态（剩余天数、到期时间等） |
| `/afdian_help` | 显示本帮助信息 |

### 🛡 管理指令

| 命令 | 说明 |
| --- | --- |
| `/afdian_reset <订单号>` | 释放指定订单的绑定状态 |
| `/afdian_reset_all YES` | ⚠️ 一键清除所有持久化数据（除插件配置外） |
| `/afdian_query <订单号>` | 查询指定订单详情 |
| `/afdian_addmodels <等级> <模型...>` | 批量添加/移动模型到指定等级（0/1/2） |
| `/afdian_delmodels [模型...]` | 批量删除模型；无参数时执行模型 API 连通性测试 |
| `/afdian_addplan <plan_id> <天数> <前缀1,前缀2,...>` | 添加赞助方案 |
| `/afdian_delplan <plan_id>` | 删除赞助方案 |
| `/afdian_getconfig` | 查看当前插件配置（敏感字段脱敏） |
| `/afdian_setconfig <key> <value>` | 设置插件配置项（白名单 + 类型校验） |
| `/afdian_migrateconfig` | 一次性迁移旧版 sp 配置到官方配置 |

### 💡 使用流程

1. 在爱发电赞助并获取订单号
2. 使用 `/afdian_bind <订单号>` 绑定
3. 使用 `/afdian_models` 查看可用模型
4. 使用 `/afdian_switch <模型名>` 切换模型

### 群聊切换模型

群聊中发送 `/afdian_switch <模型名>`，系统会先验证发送者是否为群主或群管：

- 优先通过平台下发的群角色信息查询（`group_owner` / `group_admins`）
- 平台信息不可用时使用 WebUI 配置的静态管理员列表（`group_admins`）
- 非群管直接拒绝

## 订单处理流程

纯 API 模式，不依赖 Webhook：

1. 用户通过 `/afdian_bind <订单号>` 手动绑定 → 调用 API 校验订单
2. 定时轮询爱发电 API（间隔由 `poll_interval_hours` 配置，默认 1 小时），翻页查询新订单 → 自动为已绑定用户累加天数
3. 校验订单状态 `status=2`（已支付）、去重（`out_trade_no`）
4. plan_id 命中方案映射才给权限
5. 同爱发电 user_id 多次购买累加天数

## 自动任务

- **每日零点**：遍历所有绑定，天数减 1，到期自动降级并把会话 provider 恢复为全局默认模型
- **定时轮询**：调用爱发电 API 翻页查询订单，发现新订单自动标记（已绑定用户自动累加）

## 存储结构

所有持久化数据基于 AstrBot sp（SQLite），无需维护额外文件：

- `afdian_model:processed_orders`：已处理订单号集合（去重）
- `afdian_model:plan_mapping`：`{plan_id: {days, prefixes}}`（含 `_auto_` 前缀的配置自动方案）
- `afdian_model:group_admins`：不再使用，改为 WebUI 配置项
- `afdian_model:active_umos`：活跃 UMO 索引列表
- `afdian_model:umo:<umo_json>`：`{remaining_days, prefixes, expire_time, plan_id, l1_days, l2_days, active_level, used_orders}`
- `afdian_model:umo:<umo_json>:current`：当前会话使用的模型
- `afdian_model:by_afdian:<afdian_user_id>`：用户 → umo_key 映射
- `afdian_model:user_index`：用户索引列表

插件配置存放在 AstrBot 官方配置文件（`data/config/astrbot_plugin_afdian_model_config.json`），以 WebUI 为唯一编辑入口；`/afdian_setconfig` 为白名单内的快捷入口。

## 依赖

- `aiohttp`：异步 HTTP 请求（直接调用爱发电 API）

## 许可证

本项目基于 **GNU Affero General Public License v3.0 (AGPL-3.0)** 开源。

> 本项目是 [AstrBot](https://github.com/Soulter/AstrBot) 的插件，母项目 [AstrBot](https://github.com/Soulter/AstrBot) 同样采用 AGPL-3.0 协议。根据 AGPL-3.0 第 13 条，若您修改本插件并通过网络提供服务，必须向用户公开修改后的源代码。
