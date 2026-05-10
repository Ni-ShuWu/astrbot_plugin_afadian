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

## 管理员命令

以下命令仅 AstrBot 管理员可用：

| 命令                                             | 说明           |
| ---------------------------------------------- | ------------ |
| `/afdian_addplan <plan_id> <天数> <前缀1,前缀2,...>` | 添加赞助方案映射     |
| `/afdian_delplan <plan_id>`                    | 删除赞助方案       |
| `/afdian_addadmin <群号> <QQ号>`                  | 添加群管理员（静态兜底） |
| `/afdian_deladmin <群号> <QQ号>`                  | 移除群管理员       |

### 示例

```
/afdian_addplan plan_gpt4_30d 30 gpt-4o,gpt-4
/afdian_addplan plan_claude_30d 30 claude-3
/afdian_addadmin 123456789 10001
```

## 用户命令（仅私聊）

| 命令                          | 说明               |
| --------------------------- | ---------------- |
| `/afdian_bind <订单号>`        | 绑定爱发电订单，获得模型权限   |
| `/afdian_models`            | 查看当前可用的模型列表      |
| `/afdian_switch <前缀> <模型名>` | 切换当前使用的模型        |
| `/afdian_status`            | 查看剩余天数、当前模型、到期时间 |

### 群聊切换模型

群聊中发送 `/afdian_switch <前缀> <模型名>`，系统会先验证发送者是否为群主或群管：

- 优先通过平台 API 查询角色（owner/admin）
- 平台 API 不可用时使用静态管理员列表兜底
- 非群管直接拒绝

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

- `afdiankit`：爱发电 Python SDK（API 签名、类型安全）

