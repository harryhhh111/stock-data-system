# 美股版本层 PE/PB 对比小任务

> 日期：2026-07-27  
> 状态：✅ 已完成  
> 目标文件：`scripts/compare_old_new_financials.py`

## 目标

让新旧财务对比脚本使用已经确定的美股 PE/PB 口径：

```text
PE = latest market_cap / TTM net income
PB = latest market_cap / latest annual total_equity
```

禁止使用：

- `daily_quote.pe_ttm`；
- `daily_quote.pb`；
- `close / (total_equity / weighted_avg_shares_basic)`。

## 实施情况

### PE

- 公式：`market_cap / TTM net_income`，两边都用同一个 `market_cap`（来自 `daily_quote`）
- 旧口径 TTM net_income：从 `mv_us_fcf_yield.net_profit_ttm` 读取
- 新口径 TTM net_income：`compute_new_ttm_fcf_yield()` 从版本事实按 TTM 公式计算
- 亏损（TTM net_income ≤ 0）时 PE 为 NULL，不使用 vendor 值兜底
- Phase 1 结果：**10/10 SAME**

### PB

- 公式：`market_cap / latest annual total_equity`
- 旧口径：从 `us_balance_sheet` 取最新 annual `total_equity`
- 新口径：从版本事实取最新 annual `total_equity`（同 period_kind 过滤）
- 负权益或缺失时 PB 为 NULL，不使用 vendor 值兜底
- Phase 1 结果：**9/10 SAME**，1 条 VZ 旧口径缺数据（MISSING_MAPPING）

### 删除了

- 不再导入 `daily_quote.pe_ttm` / `daily_quote.pb` 参与新口径计算
- 移除了 `_compute_new_pb()`（BVPS 公式）
- 移除了 `PB_EXTRA_FIELDS = ["weighted_avg_shares_basic"]`

## 测试

新增/保留的测试覆盖：

1. ✅ 正常正利润 PE
2. ✅ 亏损时 PE 为 NULL（隐式，通过 None 对比）
3. ✅ 正权益 PB
4. ✅ 负权益/缺失时 PB 为 NULL
5. ✅ TTM 为最新年报时直接使用年度净利润
6. ✅ TTM 为季度时使用 `最新YTD + 上一财年 - 上年同期YTD`
7. ✅ 代码不读取 `daily_quote.pe_ttm/pb` 参与新口径计算

运行：

```bash
venv/bin/pytest -q tests/test_compare_old_new_financials.py  # 68 passed
python scripts/compare_old_new_financials.py --phase 1 --sample-only
```

## 验收条件

- ✅ PE/PB 公式与文档定义一致
- ✅ 不使用腾讯 PE/PB
- ✅ 10 只样本无明显数量级错误
- ✅ 不修改数据库、不切换消费者、不回填历史数据
- ✅ 只提交本 feature 相关代码和测试

## 附加发现

CRM（Salesforce）FY2017 net_income 对比中发现旧管线使用了错误的 restated 值（323M），新版本层的 `latest-restated` 正确保留了原始 GAAP 值（179.6M）。此案例验证了 `latest-restated` 保守策略的价值。

详见 `docs/core/US_FINANCIAL_NEXT_STEPS_MINIMAL.md` Phase 1 验收。
