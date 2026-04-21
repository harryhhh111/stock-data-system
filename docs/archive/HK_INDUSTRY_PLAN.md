# 港股行业分类回填方案

## 背景

当前 `stock_info` 表中港股（`market='CN_HK'`）的 `industry` 字段全部为空，共 2743 只（其中 2637 只有行情数据）。

A 股已有申万一级行业分类，美股已有 SEC EDGAR SIC Code，唯独港股缺失，导致跨市场筛选和分析无法按行业过滤。

## 数据源

### 东方财富港股 F10 接口

- **接口地址：** `https://emweb.securities.eastmoney.com/PC_HKF10/CompanyProfile/PageAjax?code={stock_code}`
- **方法：** GET
- **关键字段：** `gszl.sshy`（上市行业，港交所官方分类标准）
- **网络要求：** 直连可用，**不需要代理**
- **认证：** 无，无需 API Key

### 行业分类标准

使用港交所官方行业分类，与 A 股申万一级不一致。典型行业值：

| 港交所行业 | 申万一级（参考） |
|-----------|----------------|
| 银行 | 银行 |
| 保险 | 非银金融 |
| 地产 | 房地产 |
| 软件服务 | 计算机 |
| 电讯 | 通信 |
| 公用事业 | 公用事业 |
| 药品及生物科技 | 医药生物 |
| 食物饮品 | 食品饮料 |
| 汽车 | 汽车 |
| 建筑 | 建筑装饰 |
| 煤炭 | 煤炭 |
| 石油及天然气 | 石油石化 |
| 综合企业 | — |
| 工业工程 | 机械设备 |
| 专业零售 | 商贸零售 |
| 资讯科技器材 | 电子 |
| … | … |

**不对齐方案：** 暂不建立映射表，各自保留原始分类。筛选时按各市场自己的行业值过滤。如果后续需要跨市场统一，再建映射表。

### 验证结果

- 10 只样本测试：全部成功，0 失败
- 单次请求响应时间：~0.1s
- 行业字段覆盖率：100%（所有测试样本均有 `sshy` 字段）

## 实现方案

### 接口流程

```
1. 从 stock_info 查询 market='CN_HK' 的所有股票代码
2. 逐只请求东方财富 F10 接口
3. 解析 JSON，提取 gszl.sshy（行业）字段
4. UPDATE stock_info SET industry = {sshy} WHERE stock_code = {code} AND market = 'CN_HK'
```

### 请求策略

- **间隔：** 随机抖动 3~8 秒（基准 0.3~0.8s × 10 倍）
- **超时：** 单次请求 15 秒
- **重试：** 失败后最多重试 3 次，间隔递增
- **预计总耗时：** 2637 只 × 平均 5.5s ≈ **4 小时**

### 错误处理

- 单只失败不中断整体流程，记录失败列表
- 支持断点续传：跳过 `industry` 已非空的记录
- 失败重试：指数退避（失败后等 5s、10s、20s 再重试）

### 代码结构

在 `fetchers/industry.py` 中新增函数：

```python
def fetch_hk_industry(
    stocks: list[dict[str, str]],
    delay_range: tuple[float, float] = (3.0, 8.0),
    max_retries: int = 3,
) -> list[dict[str, str]]:
    """从东方财富 F10 获取港股行业分类。

    Args:
        stocks: [{"stock_code": "00700"}, ...]
        delay_range: 请求间隔随机范围（秒）
        max_retries: 单只股票最大重试次数

    Returns:
        [{"stock_code": "00700", "industry_name": "软件服务"}, ...]
    """
```

在 `db.py` 中新增行业写入函数：

```python
def update_stock_industry(stock_code: str, market: str, industry: str) -> None:
    """更新 stock_info.industry 字段。"""
```

### CLI 入口

通过 `sync.py` 新增子命令：

```bash
# 全量回填（跳过已有行业的记录）
python sync.py --industry-hk

# 强制全量回填（覆盖已有记录）
python sync.py --industry-hk --force
```

### 运行策略

- **频率：** 一次性回填，后续无需定期运行
- **增量更新：** 新港股上市时，可手动运行一次补行业
- **不做定时任务：** 行业是静态数据，不需要纳入 scheduler

## 风险与应对

| 风险 | 概率 | 应对 |
|------|------|------|
| 东方财富封 IP | 低（F10 接口非行情接口） | 3~8 秒随机间隔降低风险 |
| `sshy` 字段为空 | 极低 | 记录空值但不阻断流程 |
| 请求超时 | 低 | 15s 超时 + 3 次重试 |
| 4 小时运行中断 | 中 | 断点续传（跳过已有 industry 的记录） |

## 工作量估算

- 代码开发：~1 小时（新增 1 个 fetch 函数 + 1 个 db 函数 + CLI 子命令）
- 实际运行：~4 小时（受限于请求间隔）
- 测试验证：~15 分钟
