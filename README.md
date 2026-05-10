# 爱发电模型订阅 (astrbot_plugin_afdian_model)

基于爱发电赞助的 LLM 模型自助切换插件。用户通过爱发电赞助后获得模型选择权，可在私聊中自助切换 LLM 模型。群聊中仅群主/群管可为群切换模型。权限到期自动收回。

## 安装

1. 在 AstrBot 管理面板上传插件 zip 包
2. 重启 AstrBot 或在插件管理中启用本插件
3. 依赖自动安装（`aiohttp`, `pycryptodome`）

## 配置

在 AstrBot WebUI 插件管理 → 本插件 → 配置：

| 配置项 | 说明 |
|--------|------|
| `afdian_user_id` | 爱发电用户ID，在 [爱发电开发者后台](https://afdian.net/dashboard/dev) 获取 |
| `afdian_token` | 爱发电 API Token |
| `afdian_public_key` | Webhook RSA 公钥（PEM格式），不用 Webhook 可留空 |
| `model_list` | 模型名称列表，一行一个 |

## 管理员命令

以下命令仅 AstrBot 管理员可用：

| 命令 | 说明 |
|------|------|
| `/afdian_addplan <plan_id> <天数> <前缀1,前缀2,...>` | 添加赞助方案映射 |
| `/afdian_delplan <plan_id>` | 删除赞助方案 |
| `/afdian_addadmin <群号> <QQ号>` | 添加群管理员（静态兜底） |
| `/afdian_deladmin <群号> <QQ号>` | 移除群管理员 |

### 示例

```
/afdian_addplan plan_gpt4_30d 30 gpt-4o,gpt-4
/afdian_addplan plan_claude_30d 30 claude-3
/afdian_addadmin 123456789 10001
```

## 用户命令（仅私聊）

| 命令 | 说明 |
|------|------|
| `/afdian_bind <订单号>` | 绑定爱发电订单，获得模型权限 |
| `/afdian_models` | 查看当前可用的模型列表 |
| `/afdian_switch <前缀> <模型名>` | 切换当前使用的模型 |
| `/afdian_status` | 查看剩余天数、当前模型、到期时间 |

### 群聊切换模型

群聊中发送 `/afdian_switch <前缀> <模型名>`，系统会先验证发送者是否为群主或群管：
- 优先通过平台 API 查询角色（owner/admin）
- 平台 API 不可用时使用静态管理员列表兜底
- 非群管直接拒绝

## Webhook

- 插件自动启动独立 aiohttp 服务，默认监听端口 **6199**
- 路由地址：`POST /api/v1/afdian/webhook`
- 使用 RSA+SHA256 验证爱发电签名
- 在爱发电开发者后台将 Webhook URL 配置为 `http://<你的服务器IP>:6199/api/v1/afdian/webhook`
- 端口可在插件配置界面修改

## 自动任务

- **每日零点**：遍历所有绑定，天数减 1，到期自动切回默认模型并清除绑定
- **每 6 小时**：调用爱发电 API 翻页查询订单，发现新订单自动处理

## 爱发电 API 对接

### 签名公式

```
sign = md5(token + "params" + json_str + "ts" + ts + "user_id" + user_id)
```

ts 误差 3600 秒内有效。

### 订单处理流程

1. 验签 / 验 RSA 签名
2. 幂等去重（out_trade_no）
3. 校验 status=2（已支付）
4. plan_id 命中 plan_mapping 才给权限
5. 同爱发电 user_id 多次购买累加天数

## 存储结构

- `processed_orders.json`：已处理订单号集合，仅用于去重
- `sp` 存储：
  - `afdian_model:plan_mapping` → `{plan_id: {days, prefixes}}`
  - `afdian_model:group_admins` → `{group_id: [qq_numbers]}`
  - `afdian_model:active_umos` → 活跃 UMO 索引列表
  - `afdian_model:umo:<umo_json>` → `{remaining_days, prefixes, expire_time, plan_id}`
  - `afdian_model:umo:by_afdian:<afdian_user_id>` → umo_key 映射

## 依赖

- `aiohttp`：AstrBot 框架依赖
- `pycryptodome`：RSA 签名验证
- `afdiankit`：爱发电 Python SDK（自动签名、类型安全）
