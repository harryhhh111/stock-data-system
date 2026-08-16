# 海外服务器部署文档（美股专项）

## 概述

海外服务器只负责美股数据同步，与国内服务器完全独立。

| | 国内服务器 | 海外服务器 |
|--|-----------|-----------|
| 市场 | CN_A, CN_HK | US |
| 数据源 | 腾讯 qt.gtimg.cn | SEC EDGAR, Wikipedia |
| PostgreSQL | stock_data 库 | stock_data 库（独立） |
| Scheduler | A股 16:37/17:07, 港股 17:12/17:37 | 美股 05:37 |

## 1. 系统依赖

```bash
# PostgreSQL 16
sudo apt install -y postgresql-16

# Python 3.12
sudo apt install -y python3.12 python3.12-venv python3-pip

# Git
sudo apt install -y git
```

## 2. 部署代码

```bash
cd /home/ubuntu
git clone https://github.com/harryhhh111/stock-data-system.git projects/stock_data
cd projects/stock_data
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. 配置数据库

```bash
# 创建数据库和用户
sudo -u postgres psql <<EOF
CREATE USER stock_user WITH PASSWORD '你的密码';
CREATE DATABASE stock_data OWNER stock_user;
EOF

# 初始化表结构
cd /root/stock_data
source venv/bin/activate
python3 -c "from db import init_db; init_db()"
```

## 4. 配置文件

项目根目录创建 `.env` 文件：

```env
# 数据库（本地 PostgreSQL）
STOCK_DB_HOST=localhost
STOCK_DB_PORT=5432
STOCK_DB_NAME=stock_data
STOCK_DB_USER=stock_user
STOCK_DB_PASSWORD=你的密码

# SEC EDGAR
STOCK_SEC_USER_AGENT=stock-data-system contact@example.com

# 市场过滤（海外服务器只跑美股）
STOCK_MARKETS=US
```

`STOCK_MARKETS` 是逗号分隔的市场列表，控制 scheduler.py 注册哪些任务：

| 值 | 注册的任务 |
|----|-----------|
| `CN_A,CN_HK` | A股 + 港股行情/财务同步 |
| `US` | 美股行情/财务同步 |
| 未设置 | 无任务注册，scheduler 启动后警告退出 |

如果 `config.py` 是从环境变量读取的，确认以上变量名与代码一致。如果不是，可能需要改 `config.py`。

## 5. 初始化美股数据

```bash
cd /home/ubuntu/projects/stock_data
source venv/bin/activate

# 导入 S&P 500 股票列表
python -m core.sync --type stock_list --market US

# 拉取财务数据（income_statement, balance_sheet, cash_flow_statement）
python -m core.sync --type financial --market US

# 拉取美股行业分类（SEC SIC Code）
python -m core.sync --type industry --market US

# 拉取实时行情
python -m core.sync --type daily --market US
```

每一步可能需要较长时间（503 只 × SEC EDGAR 限流），建议分开跑。

## 6. 定时任务

```bash
cd /home/ubuntu/projects/stock_data

# 创建 systemd 服务
sudo tee /etc/systemd/system/stock-scheduler.service <<EOF
[Unit]
Description=Stock Data Scheduler
After=network.target postgresql.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/projects/stock_data
EnvironmentFile=/home/ubuntu/projects/stock_data/.env
ExecStart=/home/ubuntu/projects/stock_data/venv/bin/python -m core.scheduler
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable stock-scheduler
sudo systemctl start stock-scheduler
```

## 7. 验证

```bash
# 检查服务状态
sudo systemctl status stock-scheduler

# 手动跑一次同步测试
cd /home/ubuntu/projects/stock_data
source venv/bin/activate
python -m core.sync --type daily --market US
```

## 8. 对象存储（COS 归档挂载）

旧对象退役归档（Phase E-0 起）使用腾讯云 COS，经 cosfs 以 FUSE 方式挂载到本机：

| 项 | 值 |
|---|---|
| 存储桶 | `stock-data-1253228291` |
| 本机挂载点 | `/lhcos-data`（`mount \| grep cosfs` 应在线） |
| 桶内挂载前缀 | `/stock-data-backups`（即 `/lhcos-data` 根目录对应该前缀） |
| cosfs 挂载参数 | 分块 10MB、并发 10 |
| 访问方式 | 工具统一使用 `file:///lhcos-data`，凭证由 cosfs 配置管理，不入命令行/代码 |

注意事项：

- **挂载失效即"假归档"**：cosfs 掉线时写入会落到本机挂载点目录而非 COS。执行归档类
  操作前必须确认 `mountpoint -q /lhcos-data` 在线；归档后核对本机磁盘余量不应因上传而
  额外减少（Phase E-0 实测判据：df 余量变化 ≈ 本地 staging 目录大小）。
- 恢复演练需要数据库账号具备 `CREATEDB`（`stock_user` 已于 2026-08-14 授予；
  该实例 pg_hba 本地行为 md5，超级用户 postgres 无已知密码，需经 root 临时改 trust
  操作后恢复，操作过程见 Phase E-0 任务文档执行记录）。

## 注意事项

- 美股财务数据来自 SEC EDGAR，网络稳定但每次请求间隔需 ≥0.5s（推荐 2 req/s）
- **scheduler.py 通过 `STOCK_MARKETS` 环境变量控制注册哪些任务**，海外服务器只需在 `.env` 中设置 `STOCK_MARKETS=US`，无需注释或修改任何代码
- 美股行情腾讯接口从海外访问延迟会稍高，但可用
- Git push/pull 在海外服务器应该很稳定
- 所有环境变量使用 `STOCK_` 前缀，由 `config.py` 自动加载
