# index-only 自动登记身份解析修复方案（R2）

日期：2026-10-24　状态：已上线（2026-10-26 补记）

## 实施记录（2026-10-26 补记）

- 2026-10-24 审核通过并实施上线（commit `4a767c9`）。
- 生产首轮验证（2026-10-24 06:12 轮次）：index-only 自动验证 40 通过 /
  1 未通过（VMRK 身份冲突——后续查实是 Equity Residential 2026-08-12
  更名 Vivmark Residential 所致，已按更名事件处置）；阻塞清单从 43
  收窄到 GFL/JBS（IFRS 主体，R3 范畴）。
- 下方为审核时方案原文，保留作历史依据。


## 背景与根因

2026-10-23 运行摘要：supplement 侧 32 只自动登记成功（已闭环）；index-only 侧
41 只自动验证 **0 通过**，全部阻塞每日财务同步。

根因（已与项目所有者确认）：`core/us_universe_registry.py::_resolve_cik`
只查 `us_security_ticker_symbol`——该表是更名/冲突策划表（全库 9 行），
不是全量 ticker→CIK 映射，index-only 新 ticker 天然无映射，全部倒在
「身份表无映射」第一道门，companyfacts 探测从未发出。而抓取路径
（`core/fetchers/us_financial.py:173`）用的是 SEC 全量
`company_tickers.json`（约 10428 家），二者身份源不一致。

## 范围

只修 R2 身份解析。明确**不做**：

- 不扩展 IFRS 解析（R3 另行讨论：沿用并完善受控不支持机制）；
- 不动 universe 生命周期 / 指数纳入流程（R1 另行讨论）；
- 不动同步失败分类与 expected_skip 语义；
- 不豁免、不摘除任何 ticker。

## 改动点

1. **统一身份源**：抽取只读 loader 读 SEC `company_tickers.json` 缓存
   （`data/sec_cache/company_tickers.json`，与 fetcher 同一文件，避免两条
   真理）。registry 只读已有缓存；缓存缺失/不可解析时本轮自动验证
   **fail-closed**：全部保持阻塞，原因注明「身份映射缓存缺失」，
   不自行发起抓取（该缓存由每日同步维护，正常必存在）。
2. **`_resolve_cik` 解析顺序**（保留并扩展现有冲突检查）：
   a. ticker 命中 `us_security_ticker_symbol` 策划表 → 沿用现有全部冲突
      规则（多 canonical、多 current、与 stock_info.cik 不一致均失败）；
   b. 否则查 `company_tickers.json` 全量映射：
      - ticker 无映射 → 保持阻塞，原因「SEC 全量映射无此 ticker」；
      - 解析出的 CIK 已被 `stock_info` 中**其他** stock_code 占用
        → 冲突失败，写明双方代码（防换码复用误登记）；
      - 通过 → 返回该 CIK。
3. **companyfacts 探测保留不变**：HTTP 200 才确认真实申报主体；
   经 `core/sec_client.py` 公共客户端占 gate 预算，每轮上限 50（覆盖 41）。
4. **区分「身份验证成功」与「财务解析可用」**：
   - 台账 evidence 只声明身份验证依据（CIK 解析无冲突 + companyfacts
     200），不声明财务解析可用性；
   - 登记只解除「未登记」阻断；登记后若抓取/解析失败（含 IFRS 主体
     0 事实写入），仍按现有分类计失败并阻塞——**不把不支持算成功**。
     GFL/JBS 预期即如此：身份可登记，ticker 级失败阻塞保留到 R3 落地。
5. **日志/摘要**：自动登记摘要拆分「身份验证通过」与「未通过（含原因
   分类计数）」，便于人工待办跟进。

## 测试

- 单测（`tests/test_us_universe_registry.py` 扩展）：
  - 策划表命中优先于全量映射；
  - 全量映射命中 → 通过；
  - CIK 被其他 stock_code 占用 → 冲突失败；
  - 缓存缺失 → fail-closed，不计入通过；
  - companyfacts 非 200 / 探测异常 → 不通过（现有语义不回退）。
- 离线重放 41 只 fixture：验证大部分可通过、未通过者带具体原因；
  GFL/JBS 登记后同步失败分类不变。
- 全量回归（显式列举路径绕开 tests/test_parsers）。

## 验证与上线

- 修复后由下一轮 06:12 财务同步自动重验 41 只（无需手工触发）；
- 预期：多数自动登记、阻塞清单显著缩短；GFL/JBS 仍以 ticker 级失败
  阻塞（留给 R3）；仍不通的带原因进人工待办；
- 本方案不含生产迁移；scheduler 需重启生效。
