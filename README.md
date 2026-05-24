# 爱发电模型订阅 (astrbot\_plugin\_afadian)

基于爱发电赞助的 LLM 模型自助切换插件。用户通过爱发电赞助后获得模型选择权，可在私聊中自助切换 LLM 模型。群聊中仅群主/群管可为群切换模型。权限到期自动收回。

## 安装

1. 在 AstrBot 管理面板上传插件 zip 包
2. 重启 AstrBot 或在插件管理中启用本插件
3. 依赖自动安装（`aiohttp`, `pycryptodome`）

## 配置

在 AstrBot WebUI 插件管理 → 本插件 → 配置：

| 配置项              | 说明                                                        |
| ---------------- | --------------------------------------------------------- |
| `afdian_user_id` | 爱发电用户ID，在 [爱发电开发者后台](https://afdian.net/dashboard/dev) 获取 |
| `afdian_token`   | 爱发电 API Token                                             |
| `model_list`     | 模型名称列表，一行一个                                               |

## 命令一览

### 📌 用户指令（仅私聊）

| 命令 | 说明 |
| --- | --- |
| `/afdian_bind <订单号>` | 绑定爱发电订单号，获得模型使用权限 |
| `/afdian_models` | 查看当前可用的模型列表 |
| `/afdian_switch <模型名>` | 切换当前使用的模型 |
| `/afdian_status` | 查看赞助权限状态（剩余天数、到期时间等） |
| `/afdian_help` | 显示本帮助信息 |

### 🔧 管理员指令

| 命令 | 说明 |
| --- | --- |
| `/afdian_reset <订单号>` | 释放指定订单的绑定状态 |
| `/afdian_reset_all YES` | ⚠️ 一键清除所有缓存和持久化数据（除插件配置外） |
| `/afdian_query <订单号>` | 查询指定订单详情 |
| `/afdian_addplan <plan_id> <天数> <前缀1,前缀2,...>` | 添加赞助方案 |
| `/afdian_delplan <plan_id>` | 删除赞助方案 |
| `/afdian_addadmin <群号> <QQ号>` | 添加群管理员 |
| `/afdian_deladmin <群号> <QQ号>` | 删除群管理员 |
| `/afdian_getconfig` | 查看当前插件配置 |
| `/afdian_setconfig <key> <value>` | 设置插件配置 |
| `/afdian_migrateconfig` | 从 AstrBot 配置迁移 |

### 📋 使用流程

1. 在爱发电赞助并获取订单号
2. 使用 `/afdian_bind <订单号>` 绑定
3. 使用 `/afdian_models` 查看可用模型
4. 使用 `/afdian_switch <模型名>` 切换模型

### 群聊切换模型

群聊中发送 `/afdian_switch <模型名>`，系统会先验证发送者是否为群主或群管：

- 优先通过平台 API 查询角色（owner/admin）
- 平台 API 不可用时使用静态管理员列表兜底
- 非群管直接拒绝

### 管理员命令示例

```
/afdian_addplan plan_gpt4_30d 30 gpt-4o,gpt-4
/afdian_addplan plan_claude_30d 30 claude-3
/afdian_addadmin 123456789 10001
/afdian_query 2025010100000001
/afdian_setconfig model_list gpt-4o,claude-3.5-sonnet
```

## 订单处理流程

纯 API 模式，不依赖 Webhook：

1. 用户通过 `/afdian_bind <订单号>` 手动绑定 → 调 API 校验订单
2. 每 6 小时自动轮询爱发电 API，翻页查询新订单 → 自动标记防止重复使用
3. 验签、幂等去重（out\_trade\_no）、校验 status=2（已支付）
4. plan\_id 命中 plan\_mapping 才给权限
5. 同爱发电 user\_id 多次购买累加天数

## 自动任务

- **每日零点**：遍历所有绑定，天数减 1，到期自动切回默认模型并清除绑定
- **每 6 小时**：调用爱发电 API 翻页查询订单，发现新订单自动标记

## 存储结构

- `processed_orders.json`：已处理订单号集合，仅用于去重
- `sp` 存储：
  - `afdian_model:plan_mapping` → `{plan_id: {days, prefixes}}`
  - `afdian_model:group_admins` → `{group_id: [qq_numbers]}`
  - `afdian_model:active_umos` → 活跃 UMO 索引列表
  - `afdian_model:umo:<umo_json>` → `{remaining_days, prefixes, expire_time, plan_id}`
  - `afdian_model:umo:by_afdian:<afdian_user_id>` → umo\_key 映射

## 依赖

- `aiohttp`：异步 HTTP 请求（直接调用爱发电 API）

> 友情提醒：本插件大部分为AI生成，不一定保证能用……


## 📄 许可证

本项目基于 **GNU Affero General Public License v3.0 (AGPL-3.0)** 开源。

> 本项目是 [AstrBot](https://github.com/Soulter/AstrBot) 的插件，母项目 [AstrBot](https://github.com/Soulter/AstrBot) 同样采用 AGPL-3.0 协议。根据 AGPL-3.0 第 13 条，若您修改本插件并通过网络提供服务，必须向用户公开修改后的源代码。
