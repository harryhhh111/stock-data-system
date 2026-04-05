# 多环境部署方案（按市场过滤任务）

## 背景

国内服务器同步 A股/港股，海外服务器同步美股。同一套代码部署到两台服务器，通过配置区分跑哪些任务，不需要注释掉任何代码。

## 方案

### 1. 环境变量

`.env` 文件新增 `STOCK_MARKETS`：

| 服务器 | STOCK_MARKETS |
|--------|--------------|
| 国内（当前） | `CN_A,CN_HK` |
| 海外（新增） | `US` |

默认值：无。如果不配置，所有任务都不注册（防止误部署）。

### 2. scheduler.py 修改

- `JOB_DEFS` 保持完整（不注释任何任务）
- 启动时读取 `STOCK_MARKETS` 环境变量，按逗号分隔解析为 market 列表
- 只注册 `job_def["market"]` 在列表中的任务
- 未配置时输出警告并退出

### 3. config.py 修改

- `SchedulerConfig` 新增 `markets` 字段，从 `STOCK_MARKETS` 环境变量读取

### 4. 部署文档更新

`docs/DEPLOY_OVERSEAS.md` 中海外服务器的 `.env` 示例：

```env
DB_HOST=localhost
DB_PORT=5432
DB_NAME=stock_data
DB_USER=stock_user
DB_PASSWORD=你的密码
SEC_USER_AGENT=stock-data-system contact@example.com
STOCK_MARKETS=US
```

国内服务器 `.env` 示例：

```env
DB_HOST=localhost
DB_PORT=5432
DB_NAME=stock_data
DB_USER=postgres
DB_PASSWORD=stock_data_2024
STOCK_MARKETS=CN_A,CN_HK
```

### 5. 兼容性

- `--once` 模式同样受 `STOCK_MARKETS` 过滤
- `--dry-run` 模式显示过滤后的任务列表
- 不影响 `sync.py` 的手动调用（`python sync.py --type daily --market US` 不受限制）
