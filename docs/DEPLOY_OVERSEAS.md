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
cd /root
git clone https://github.com/harryhhh111/stock-data-system.git stock_data
cd stock_data
python3.12 -m venv venv
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
# 数据库（本地 PostgreSQL，不需要改）
DB_HOST=localhost
DB_PORT=5432
DB_NAME=stock_data
DB_USER=stock_user
DB_PASSWORD=你的密码

# SEC EDGAR
SEC_USER_AGENT=stock-data-system contact@example.com

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
cd /root/stock_data
source venv/bin/activate

# 导入 S&P 500 股票列表
python sync.py --type stock_list --market US

# 拉取财务数据（income_statement, balance_sheet, cash_flow_statement）
python sync.py --type financial --market US

# 拉取美股行业分类（SEC SIC Code）
python sync.py --type industry --market US

# 拉取实时行情
python sync.py --type daily --market US
```

每一步可能需要较长时间（503 只 × SEC EDGAR 限流），建议分开跑。

## 6. 定时任务

```bash
cd /root/stock_data

# 创建 systemd 服务
sudo tee /etc/systemd/system/stock-scheduler.service <<EOF
[Unit]
Description=Stock Data Scheduler (US)
After=network.target postgresql.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/stock_data
ExecStart=/root/stock_data/venv/bin/python scheduler.py
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
cd /root/stock_data
source venv/bin/activate
python sync.py --type daily --market US --once
```

## 注意事项

- 美股财务数据来自 SEC EDGAR，网络稳定但每次请求间隔需 ≥0.1s
- **scheduler.py 通过 `STOCK_MARKETS` 环境变量控制注册哪些任务**，海外服务器只需在 `.env` 中设置 `STOCK_MARKETS=US`，无需注释或修改任何代码
- 如果后续想更优雅地支持按环境过滤，可以给 scheduler.py 加 `--market US` 参数，但目前注释掉是最简单的做法
- 美股行情腾讯接口从海外访问延迟会稍高，但可用
- Git push/pull 在海外服务器应该很稳定
- 数据库 `config.py` 中如果 DB_HOST 是 localhost 则不需要改
