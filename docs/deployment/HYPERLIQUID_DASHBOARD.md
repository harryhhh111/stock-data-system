# Hyperliquid 只读看板运行说明

> 2026-10-19 重写（复验整改后）。本说明覆盖 v1（测试网单 pair）与 v2（主网多 pair）两套接口；两者契约分别见 [HYPERLIQUID_DASHBOARD_MVP_PLAN.md](../HYPERLIQUID_DASHBOARD_MVP_PLAN.md) 与 [HYPERLIQUID_MULTIPAIR_PLAN.md](../HYPERLIQUID_MULTIPAIR_PLAN.md)。

## v2 多 pair（当前主线）

### 环境变量

| 变量 | 服务 | 说明 |
|---|---|---|
| `STOCK_HL_TRADER_BIN` | v1 + v2 | hl-trader 可执行文件绝对路径 |
| `STOCK_HL_PAIRS_CONFIG` | v2 | pair 目录 JSON 绝对路径（配置版本 `config_version: 1`） |
| `STOCK_HL_TRADER_CONFIG` | 仅 v1 | v1 看板最小 TOML（不含 `secret_file`） |
| `STOCK_HL_EXPECTED_ACCOUNT` | 仅 v1 | v1 部署预期测试网账户 |

v2 的 pair 网络、DEX、两腿标识与账户来自 JSON 配置，不从返回快照反推；快照身份与目录不符返回 503 且不缓存。

### pair 配置示例（地址留空待填写）

```json
{
  "config_version": 1,
  "pairs": [
    {"id": "hype-btc", "label": "HYPE / BTC", "enabled": true, "network": "mainnet",
     "dex": "", "coins": ["HYPE", "BTC"], "account_address": null, "unlock_anchors": []},
    {"id": "nbis-crwv", "label": "NBIS / CRWV", "enabled": true, "network": "mainnet",
     "dex": "xyz", "coins": ["xyz:NBIS", "xyz:CRWV"], "account_address": null, "unlock_anchors": null}
  ]
}
```

- `account_address` 为实际持仓主/子账户公开地址（0x，40 位十六进制，大小写归一）或显式 `null`；不允许占位文字、零地址或省略。不同 pair 可复用地址。
- 修改配置即改变 `config_revision`（文件字节 SHA-256），缓存命名空间自动切换；无公开写入接口，管理员原子替换文件即可。

### API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/hyperliquid/pairs` | `{schema_version:2, config_revision, pairs[]}` 目录，只读配置 |
| GET | `/hyperliquid/pairs/{pair_id}/dashboard` | 单 pair 快照；未知/停用 404，身份不符 503 |
| GET | `/hyperliquid/announcements` | 平台公告，独立缓存 key |
| GET | `/hyperliquid/dashboard` | v1 兼容接口，见下节 |

### 验证与查询命令

```sh
curl -s http://127.0.0.1:8000/api/v1/hyperliquid/pairs | head -c 400; echo
curl -s http://127.0.0.1:8000/api/v1/hyperliquid/pairs/hype-btc/dashboard | head -c 400; echo
curl -s http://127.0.0.1:8000/api/v1/hyperliquid/announcements | head -c 400; echo
```

账户未配置（`null`）时账户/仓位区块明确不可用，行情区块照常——不是故障，也不显示"空仓"。

## v1 兼容接口（测试网单 pair）

- 端点 `GET /hyperliquid/dashboard` 保持 v1 行为：测试网、单账户、五区块；块级严格校验（有限十进制、合法时间、枚举、非负约束），非法区块降级为完整不可用 DTO，不再标 ok。
- 环境变量 `STOCK_HL_TRADER_CONFIG` + `STOCK_HL_EXPECTED_ACCOUNT` 仅服务 v1。
- v1 前端页面已由 v2 总览/详情替代；接口保留用于回归与外部引用。

## 权限边界（部署门槛，非建议而是要求）

- hl-trader 看板模式不装载私钥；v2 JSON 配置与 v1 最小 TOML 均不含 `secret_file`；接口只读，不接受浏览器传入的路径或命令。
- Web 服务必须使用不同于交易程序的独立 OS 身份（如 `stock-web` 与 `hl-trader`）；不得拥有 root、交易用户组权限或切换到交易身份的能力。交易私钥仅交易身份可读，保持属主隔离与 `0o600` 或更严；Web 不继承签名环境变量。
- ACL 按用户身份判断，不能区分同一 UID 下的 Web 与交易进程。**同用户加 ACL 不是有效替代方案，不满足公开上线门槛**；ACL 仅可作为不同身份之间的附加权限控制。
- Web 身份还不得修改交易配置、状态、回执、锁文件、源码、可执行文件及可替换它们的父目录；看板固定二进制与配置由受控身份管理。v1 若读取 pair 状态，只授予必要文件读取/目录遍历权限，不授予交易目录写权限；v2 不需要交易状态权限。
- 部署前以实际 Web 运行身份，在管理员准备的假秘密和临时交易文件上验证：私钥不可读，交易文件和父目录不可写、不可替换，看板仍可运行。记录身份与验证结果；不得读取真实私钥或发送真实订单来构造测试。未完成或未通过验证不得公开上线。本次代码整改不代表系统身份及权限已经变更。

### 本轮校验整改

v1 顶层严格校验版本类型、合法非零 20 字节账户地址及 UTC 秒精度时间；预期账户配置非法时在启动子进程前拒绝查询。各区块和嵌套条目检查必填字段，显式 `null` 不等于字段缺失；非法区块降级时清除原值并使用有效快照时间。v1 资金费降级保留原字段 `annualized`，不混入 v2 的字段名。

本轮验证：本文末尾所列三份 Python 测试文件共 165 项通过（新增 56 项回归），覆盖错误顶层拒绝且不缓存、非法预期地址不启动子进程、必填字段缺失、区块时间降级及 v1/v2 契约兼容。该结果仅证明代码与离线 fixture 验证，不代表线上权限或真实账户验收完成。

## 发布与回退

1. 发布顺序：hl-trader 二进制（含 v1 `status --json` 与 v2 `dashboard` 子命令）→ 本仓库后端（`.env` 配好变量）→ 重启 web 进程（tmux `stock-web-mvp`：C-c 后 `venv/bin/python -m web`）→ 前端构建 + dist 同步国内 nginx。
2. 前端走既有 `/api/us/` 代理，不新增端口；v2 页面路径 `/hyperliquid` 与 `/hyperliquid/pairs/:pairId` 依赖 SPA fallback，nginx 需继续将所有非文件路径回退到 index.html。
3. 回退：恢复上一版二进制、后端与 dist 及旧配置；v1 接口与测试网标识保持不变，主网数据不可用于伪装 v1 读数；回退不触碰交易账本与私钥。

## 数据口径与验收边界

- 只读快照非实时行情；各区块与来源分别有查询时间；资金费窗口 `window_start`/`window_end` 恰好 48 小时，`window_complete=false` 时合计不完整并在页面标注。
- 缺值显示 `—`，不补零；页面仅首次进入与手动刷新时查询，不自动轮询/聚焦刷新。
- 公开展示（2026-10-17 确认）：任何可访问网站者可见配置账户的地址、持仓与资金费盈亏。
- 代码整改完成 ≠ 上线完成：上线还需真实地址填写、主网只读对照（两腿仓位、资金费与交易所 UI 或 API 核对并记录时间）与浏览器交互验收（桌面/移动端、正常/错误/身份切换态）。

## 验证命令

```sh
# 后端（含真实子进程与共享 fixture 契约测试，全部离线）
venv/bin/python -m pytest tests/test_web/test_hyperliquid.py tests/test_web/test_hyperliquid_pairs.py tests/test_web/test_hyperliquid_fixtures.py -q
# 前端身份、QueryObserver/刷新顺序与页面渲染回归（复用同一共享 fixture）
cd frontend && npm run test:hl
# 生产构建
cd frontend && VITE_CN_API_URL=/api/cn VITE_US_API_URL=/api/us npm run build
```
