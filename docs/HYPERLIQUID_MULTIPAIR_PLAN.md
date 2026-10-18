# Hyperliquid 多 Pair 主网只读看板实施方案

> 日期：2026-10-19（北京时间）
> 状态：已按用户确认的需求整理，供 Kimi 后续实施；本文不代表代码已完成或已部署。
> 唯一交付目标：在 stock_data 现有网站中，用一套页面查看 HYPE/BTC 与 NBIS/CRWV 各自账户的主网仓位、资金费和比价。
> 本方案定义新增 v2 接口。旧版 [单 Pair 方案](HYPERLIQUID_DASHBOARD_MVP_PLAN.md) 仅作为 v1 兼容参考；两者有冲突时，新增功能以本文为准。v1 与交易命令仍维持原有测试网边界。

## 1. 已确认需求与范围

- Hyperliquid 作为一级入口，下设总览及多个 pair 详情页；继续使用 stock_data 的网址、端口与 US 后端，不新增监听服务。
- 第一批为 HYPE/BTC、NBIS/CRWV，均读取主网。名称顺序固定比价分子/分母，不规定多空方向。
- 每个 pair 独立配置实际持仓主账户或子账户的公开地址；不同 pair 可以使用不同地址，也允许复用地址。
- 无需 API Key、API wallet 私钥或钱包签名。API wallet/agent 地址不能自动视作实际持仓账户。
- 账户地址及该 pair 仓位公开、免登录展示。配置由服务端文件维护，访客没有配置写入接口。
- 多空方向、空仓、单腿、两腿同向均按交易所实际持仓展示，不预设“第一腿多、第二腿空”。
- 本轮只读，不增加交易按钮、账户授权、转账、下单、平仓、自动再平衡或后台推送；不修改现有交易恢复逻辑。
- 包含总览、详情、比价图、资金费、HYPE 解锁与平台公告的适当归属。自动财报采集、公司新闻聚合、统计交易信号、收益回测均不在本轮。
- 主网真实地址尚未提供。示例保持 `null`，实现可用 fixture 完成；接入时由用户填写地址，不猜测、不复制测试网地址或公开示例地址。

## 2. 当前代码与数据核实

### 2.1 当前实现

核对目录：`/home/vinci/projects/hl-trader` 与 `/home/vinci/projects/stock_data`。当前 stock_data `.env` 的 `STOCK_MARKETS=US`。

| 位置 | 现状 | 本次处理 |
|---|---|---|
| hl-trader `src/pair.rs` | HYPE 多、BTC 空常量；单个 `pair-state.json` | 保留交易逻辑，v2 不读取它 |
| `src/hyperliquid.rs` | `Adapter::testnet()`；多处默认原生 DEX | 新增独立只读客户端，支持网络及 DEX，不把交易 Adapter 改成主网 |
| `src/snapshot.rs` | schema v1、HYPE/BTC 固定字段、测试网快照 | 保留 v1；新增配置驱动的 v2 DTO 与采集 |
| stock_data `web/services/hyperliquid_service.py` | 单账户环境变量、单份缓存、v1 校验 | v2 单独服务；按配置和 pair 隔离校验、缓存 |
| `frontend/src/pages/hyperliquid-page.tsx` | 单页、固定 HYPE/BTC、五张卡片 | 拆为总览、通用详情及复用卡片 |
| 前端类型与后端校验 | 部分字段未落实旧文档的 null/分源约定 | v2 必须按本文验证，不能照抄当前宽松处理 |

具体注意：当前前端直接读取数组 `.length`，但 Rust 可能返回 `null`；当前子进程输出大小是在全部捕获后才检查；当前后端可能把 stderr/本机错误原文返回浏览器。新增路径须解决这些问题，不能沿用到公开主网接口。旧路径的兼容性改动应单独说明。

### 2.2 市场身份与官方来源

2026-10-19 通过官方 `/info` 的 `meta`、`dex="xyz"` 只读查询观察到：主网存在 `xyz:NBIS`、`xyz:CRWV`；两者 `marginMode=noCross`。测试网 `xyz:NBIS` 返回 `isDelisted=true`。这些是当时的元数据，实施和上线前重新读取，不能把杠杆/保证金模式或上市状态写死。

| Pair ID | 网络 | DEX | 两腿完整标识（按比价顺序） |
|---|---|---|---|
| `hype-btc` | `mainnet` | `""`（原生） | `HYPE`, `BTC` |
| `nbis-crwv` | `mainnet` | `xyz` | `xyz:NBIS`, `xyz:CRWV` |

官方依据（本次已查阅，执行前复核接口变化）：

- [Info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)：实际账户地址、按 DEX 查询挂单、K 线及账户模式查询。
- [Perpetuals](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)：`metaAndAssetCtxs`、`clearinghouseState` 的 DEX 参数，以及 `userFunding` 的完整币种标识与分页。
- [Asset IDs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids)：HIP-3 标识使用 `{dex}:{coin}`。禁止把 `xyz:NBIS` 简化成 `NBIS` 后用于匹配。
- [Account abstraction modes](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes)：统一账户与组合保证金的余额口径不同于单 DEX 余额，不能机械累加各 DEX 权益。

## 3. 页面与交互

### 3.1 导航

| 页面 | 路径 | 行为 |
|---|---|---|
| Hyperliquid 总览 | `/hyperliquid` | 显示全部启用 pair 卡片及平台公告 |
| Pair 详情 | `/hyperliquid/pairs/:pairId` | 根据服务端目录动态加载同一详情组件 |

左侧 Hyperliquid 可展开“总览、HYPE/BTC、NBIS/CRWV”；子项来自目录接口，不在路由中逐个写死。父项与当前子项正确高亮，不能同时把总览标为选中。收起侧栏保留入口，移动端提供 pair 下拉切换。详情支持直接链接、刷新及浏览器前进/后退，顶部面包屑显示具体 pair。

### 3.2 总览

卡片显示 pair 名称、主网/测试网、市场、账户缩写及复制按钮、当前实际方向、两腿名义、两腿未实现盈亏合计、最新比价、相对日线 30MA 的位置、两腿小时费率、查询时间及错误状态。点击进入详情。

卡片按 pair 独立加载，快的先显示；某个 pair 超时不遮挡其余卡片。未配置账户时保留公开行情，明确显示“账户未配置”，不显示“空仓”。

不设置“所有 pair 总权益/总收益”数字：同一个账户或合约可能重复出现在多个 pair，不能重复加总。每张卡片的盈亏只是该地址指定两腿的未实现盈亏，不是独立策略收益或账户全部盈亏。

### 3.3 详情

从上到下：

1. 页头：pair 切换、网络、市场、实际账户地址、查询时间、手动刷新。
2. 摘要：实际方向、两腿名义、未实现盈亏合计、名义比/差异、最新比价、最近收盘比价及 30MA 偏离。
3. 比价折线图：30/90 天切换；共同 UTC 已收盘日比价与 30MA。悬停显示日期、两腿收盘价和比价。复用 ECharts；缺失点断线，不用股票现货 K 线补永续 K 线。
4. 仓位表：完整合约的友好名称、实际多/空、数量、开仓均价、mark、名义、未实现盈亏、保证金模式/占用与强平价。缺项显示“—”。单腿照实显示；两腿同向标记“同向持仓”，不宣称对冲正常。
5. 资金费：两腿小时费率、简单年化、当前持仓日收付估算、48 小时实际收付及最近 3 笔。估算与实际明确分开。
6. 相关信息：HYPE/BTC 显示人工解锁锚点；NBIS/CRWV 不展示解锁卡片，可链接现有 NBIS/CRWV 股票分析页（先核对现有路由参数），不新增财报抓取。

平台公告仅在总览展示，不在每个 pair 重复抓取。v2 不展示本地交易动作状态；可用固定文案“只读观察，未接入执行状态”，这不是数据故障。

切换 pair 时不得以 A 的旧读数作为 B 的占位数据；同一 pair 刷新可以保留旧数据并提示失败及旧时间。沿用主题与移动端布局，宽表支持横向滚动。默认只在首次进入和手动刷新时查询，禁用自动轮询和窗口聚焦触发刷新。

## 4. Pair 配置：每组独立地址

新增服务端 JSON 配置，环境变量 `STOCK_HL_PAIRS_CONFIG` 指向绝对路径；`STOCK_HL_TRADER_BIN` 继续指定固定可执行文件。Python 与 Rust 读取同一份配置，减少两仓库的映射漂移。JSON 由两侧标准库/现有依赖解析，不新增数据库。

```json
{
  "config_version": 1,
  "pairs": [
    {
      "id": "hype-btc",
      "label": "HYPE / BTC",
      "enabled": true,
      "network": "mainnet",
      "dex": "",
      "coins": ["HYPE", "BTC"],
      "account_address": null,
      "unlock_anchors": []
    },
    {
      "id": "nbis-crwv",
      "label": "NBIS / CRWV",
      "enabled": true,
      "network": "mainnet",
      "dex": "xyz",
      "coins": ["xyz:NBIS", "xyz:CRWV"],
      "account_address": null,
      "unlock_anchors": null
    }
  ]
}
```

- `id` 唯一、稳定，匹配 `^[a-z0-9]+(?:-[a-z0-9]+)*$`，最长 64；不是路径。`label` 为非空展示名。
- `network` 只接受 `mainnet|testnet`，固定映射官方 URL，无任意 URL 配置。第一批两组固定使用主网；测试网必须是另一个独立配置条目，禁止自动回退。
- `coins` 恰好两个不同的完整永续标识；顺序就是比价方向。每个 pair 本轮限定同一 DEX；原生无前缀，HIP-3 前缀必须与 `dex` 一致，并与实时 metadata 核对。本轮金额口径仅支持 USDC 抵押/结算的这两组市场；以后增加其他抵押币的 pair 须先扩展货币字段，不能继续硬标 USDC。
- `account_address` 为合法非零 20 字节 EVM 地址，统一小写，或显式 `null`。不允许省略后自动继承全局账户；空串、示例占位文字是错误。允许不同 pair 复用同一地址。
- `unlock_anchors=null` 表示不适用；`[]` 表示适用但尚未配置。输入条目字段固定为 `date_utc,label,source_url,verified_utc`（字符串），沿用 v1 人工锚点的日期、来源及核实规则；输出将 `date_utc` 映射成 `date`。
- 两侧拒绝重复 JSON 键、重复 ID、未知字段和错误类型。禁止秘密、交易参数、状态目录及执行权限出现在 v2 配置中。
- 配置更新由管理员修改文件并原子替换，无公开 POST/PUT 接口。后端每次请求计算配置原始字节的 SHA-256（小写十六进制 `config_revision`），配置改变即切换缓存命名空间；读取/校验失败明确返回 503，不继续服务旧账户缓存。
- `STOCK_HL_TRADER_CONFIG`、`STOCK_HL_EXPECTED_ACCOUNT` 仅服务旧 v1；v2 的预期账户与网络来自所选 pair 配置，不能从返回快照反推。

## 5. 跨仓库接口与只读边界

### 5.1 Rust 新入口

保留所有现有命令与 `status --json` 的 v1 行为，新增：

```sh
hl-trader --config /固定路径/hl-dashboard-pairs.json dashboard --pair nbis-crwv --json
hl-trader --config /固定路径/hl-dashboard-pairs.json dashboard --announcements --json
```

`--pair` 与 `--announcements` 二选一。新命令使用独立配置解析器和只读 info 客户端，不通过交易 `load_config`，不实例化签名器、不读取 agent 私钥或 `receipt_dir`，不获取交易锁、不写回执。`--config` 在这个命令下是 JSON；其他命令仍是原来的 TOML。

只读客户端封装固定网络的 `/info` 请求，不暴露 `/exchange` 或签名方法；官方公告复用现有固定来源解析器。复用 Decimal、日线对齐和纯计算函数，避免将 `LONG_COIN/SHORT_COIN` 或默认测试网带入新入口。当前交易 Adapter、`NETWORK` 常量与开平仓命令不因看板主网支持而放开。

账户、挂单、行情、历史等独立查询并保留各自状态；同次快照中共享已取得的源数据，避免两个区块重复查询。每个外部请求设时限，整体任务须能在 Python 的 30 秒期限内结束或受控失败。

### 5.2 FastAPI

路径均位于现有 `/api/v1` 下，成功沿用 `{ "ok": true, "data": ... }`：

| GET 路径 | 返回及用途 |
|---|---|
| `/hyperliquid/pairs` | `{schema_version:2, config_revision:string, pairs:PairDescriptor[]}`；只读配置目录，不等交易所 |
| `/hyperliquid/pairs/{pair_id}/dashboard` | 单个 v2 快照，供卡片与详情共用 |
| `/hyperliquid/announcements` | `{schema_version:2, queried_at:string, block:AnnouncementsBlock}`；平台公告独立缓存 |
| `/hyperliquid/dashboard` | 保持 v1，不悄悄改成主网或返回 v2 |

`PairDescriptor` 字段为 `id,label,network,dex,coins,account_address,unlocks_enabled`，分别来自已校验配置；`unlocks_enabled` 为 `unlock_anchors !== null`。目录不返回配置路径或原始配置。

浏览器仅传目录中的 `pair_id`，不接受地址、URL、命令或文件路径覆盖。未知/停用 pair 返回 404；配置非法、进程失败、快照身份不符返回 503。一个 pair 返回 503 不影响目录或其他 pair。

每次执行使用参数数组、禁用 shell。实时限制 stdout 与 stderr 的捕获内存（各最多 1 MiB），不能先无界 `capture_output` 再检查；30 秒超时/超限后终止并回收进程。退出非零不解析 stdout。外部响应不包含原始 stderr、绝对路径、原始配置或 traceback；服务端记录内部原因。

后端按 `(schema_version, config_revision, network, normalized_account_or_null, dex, ordered_coins, pair_id)` 缓存有效快照 30 秒，同 key 请求合并，不用一个全局锁串行运行所有 pair。最多同时运行 2 个采集子进程，公告使用独立 key；失败不写入成功缓存，清除已失效内容，缓存命中不改写任何时间。

前端 query key 至少含 `config_revision` 和 `pair_id`；配置换账户后即使 ID 不变也不得保留旧账户读数。刷新先重取目录，使用新 revision；详情响应的账户/网络/两腿不符合当前目录时先刷新目录再展示，不能混用新标题与旧数据。

### 5.3 v2 快照契约

以下是字段定义，不是可直接发布的行情示例。类型中的 `DecimalString` 是有限十进制字符串，不接受 NaN、Infinity、空串或 JSON number；`Timestamp` 是 UTC RFC3339 秒精度 `Z` 结尾，`Date` 是 `YYYY-MM-DD`。

```typescript
type DecimalString = string;
type Timestamp = string;
type DateString = string;
type Status = 'ok' | 'partial' | 'unavailable';
type SourceQuery = {
  status: 'ok' | 'unavailable';
  queried_at: Timestamp | null; // null 仅表示未尝试，例如账户未配置
};
type Block<T> = T & {
  status: Status;
  queried_at: Timestamp;
  source_queries: Record<string, SourceQuery>;
  diagnostics: string[];
};
type PairDescriptor = {
  id: string; label: string; network: 'mainnet' | 'testnet';
  dex: string; coins: [string, string]; account_address: string | null;
  unlocks_enabled: boolean;
};
type Position = {
  coin: string; side: 'long' | 'short'; size: DecimalString;
  entry_px: DecimalString | null; mark_px: DecimalString | null;
  notional: DecimalString | null; unrealized_pnl: DecimalString | null;
  margin_mode: 'cross' | 'isolated' | null;
  margin_used: DecimalString | null; liquidation_px: DecimalString | null;
};
type PairSnapshot = {
  schema_version: 2; config_revision: string; queried_at: Timestamp; pair: PairDescriptor;
  blocks: {
    account: Block<{
      account_mode: string | null; // 官方 userAbstraction 原始枚举，见下文
      equity: DecimalString | null;
      equity_scope: 'perp_dex' | 'account' | 'unavailable';
    }>;
    positions: Block<{
      items: Position[] | null;
      position_state: 'flat' | 'single_leg' | 'opposite' | 'same_direction' | 'unknown';
      unrealized_pnl: DecimalString | null; // 指定两腿合计
      notional_ratio: DecimalString | null;
      ratio_drift_pct: DecimalString | null;
      open_orders: number | null;
    }>;
    funding: Block<{
      hourly_rates: Record<string, DecimalString | null>;
      annualized_pct: Record<string, DecimalString | null>;
      daily_estimate: {direction:'receive'|'pay'|'flat'; amount:DecimalString} | null;
      window_start: Timestamp; window_end: Timestamp; window_complete: boolean;
      actual_48h_net: DecimalString | null;
      recent_payments: {time:Timestamp; coin:string; usdc:DecimalString; rate:DecimalString}[] | null;
    }>;
    ratio: Block<{
      live: DecimalString | null;
      latest_close: DecimalString | null; as_of_date: DateString | null;
      ma30: DecimalString | null; vs_ma30_pct: DecimalString | null;
      points: {date:DateString; first_close:DecimalString|null; second_close:DecimalString|null;
               ratio:DecimalString|null; ma30:DecimalString|null}[] | null;
    }>;
    unlocks: Block<{
      anchors: {date:DateString; days_ahead:number; label:string; source_url:string; verified_utc:DateString}[];
      note: string;
    }> | null;
  };
};
```

共用规则：

- `schema_version` 必须精确为整数 2。`pair` 的全部字段须与服务端当前目录相符；地址先验证再小写比较，合约大小写与前缀保持 metadata 原值。身份不一致整份拒绝且不缓存。Rust 对自己实际读取的配置原始字节计算 `config_revision`；Python 与本次调用预期摘要比较，并在返回/写缓存前确认配置未改变。文件在调用中途被替换时返回明确的重试错误，不将旧快照放入新配置缓存。
- 五个 block 键固定存在；只有 `unlocks_enabled=false` 时 `unlocks=null`。不得把无配置账户映射成另一个账户。无账户时账户/仓位数据不可用，行情照常。
- `ok` 表示适用数据完整有效，diagnostics 为空；`partial` 表示仍有有效数据但部分缺失/过期；`unavailable` 表示该块无可展示结果。区块状态不代表交易成功或投资判断。
- 顶层时间为快照生成时间，block 时间为处理结束时间，source 时间为该次查询完成/失败时间。缓存不得更新时间；未经查询不伪造源时间。
- 固定来源键：account=`mode,balances`；positions=`positions,orders,market`；funding=`positions,market,user_funding`；ratio=`market,candles_first,candles_second`；unlocks=`config`；公告=`general,api`。同次共享来源的时间保持一致。
- source 的失败描述放 block diagnostics，缺值一律 null。成功查得零与失败必须区分。Python 校验字段、完整合约归属、枚举、非负约束、真实日期和有限十进制；错误 block 转成完整的 unavailable 结构，不向前端透传未验证值。全部适用 block unavailable 时 503。
- 前端按 DTO 正确处理 null；不能为了避免崩溃，把查询失败的 `items=null` 改成正常空仓 `[]`。

平台 `AnnouncementsBlock` 沿用 v1 的 `items[{kind,text,published_at,url,source}]` 与共用 block 字段，发布时间为 UTC RFC3339 或 null，固定来源、纯文本摘要、不渲染远端 HTML。

## 6. 数据采集与计算口径

### 6.1 账户及仓位

- 账户查询使用配置地址；`clearinghouseState`、`openOrders` 按 pair DEX 查询，行情通过同 DEX metadata/context 查询。完整 coin 精确过滤两腿。账户中其他合约不进入 pair 仓位、挂单、盈亏和资金费合计。
- 查询成功且两腿均无非零仓位才是 `items=[]`、`position_state=flat`、两腿未实现盈亏为 `"0"`。仓位失败为 `items=null`、state unknown。仅一腿有仓位为 single_leg；两腿反向为 opposite；同向为 same_direction。
- `size=abs(szi)`，正 szi 为 long、负为 short。不得将相反方向视为“策略错误”；不以合约顺序决定买卖。零数量不生成行。
- mark 缺失/非正不计算名义，保留已知数量与交易所盈亏。名义为绝对数量 × 正 mark；两腿名义都正才计算第一腿/第二腿名义比，以及 `abs(N1-N2)/((N1+N2)/2)*100` 的百分数差异。沿用比值 4 位、差异 2 位展示，计算前不舍入。差异不表示 beta 中性或风险等级。
- 盈亏合计仅在两腿实际仓位已确认、所有持仓行的盈亏均有效时计算；缺失不能当 0。保留交易所开仓均价与强平价，不自行推导组合强平价或将资金费加入“未实现盈亏”。
- `userAbstraction` 当前枚举为 `unifiedAccount|portfolioMargin|disabled|default|dexAbstraction`；未知值保留诊断，不猜模式。标准账户可提供经过验证的该 DEX `marginSummary.accountValue`，scope=`perp_dex`，页面标“原生/xyz 永续市场权益”，不能标成整个钱包权益。
- 统一账户/组合保证金不能直接套用单 DEX 权益。本轮不自行计算全账户总权益：无法确认口径时 equity=null、scope=unavailable 并说明，仍展示经验证的实际持仓。账户模式/权益失败不应丢弃独立查询成功的持仓与资金费。实施中必须用这两类账户响应 fixture 验证持仓字段，不能把不适用的余额摘要当正常零余额。
- v2 不读 v1 本地 PairStore，不把任何测试网状态挂到主网账户上。未来交易接入另行定义执行状态和账户授权。

### 6.2 资金费

- 两个费率 map 的 key 必须恰好是配置完整 coins，不固定 HYPE/BTC，不去掉 `xyz:`。小时费率使用接口自然小数；`annualized_pct=rate*24*365*100`，不是复利预测。
- 日估算净收付 `net=-sum(signed_size * oracle_px * hourly_rate * 24)`；正为 receive，负为 pay，零为 flat，amount=abs(net)。持仓已确认全空时 daily_estimate=null；任一实际持仓所需价格/费率缺失时也为 null 并诊断。未持仓币种的费率缺失不能影响另一条实际持仓的可计算估算。
- `userFunding` 使用实际账户与固定 `[start,end)` 48 小时窗口，按返回的完整 coin 筛选。API 未声明的 `dex` 参数不得凭空加入资金费查询。
- 以原始毫秒时间分页、去重和过滤，再序列化秒级展示时间。重复边界不能漏掉同毫秒不同记录；去重键须包含网络、账户、coin、时间及源标识（不能只用可能跨币种重复的 hash）。达到上限、分页无法推进或中断都不能声称完整。
- 完整成功且无记录才返回 net="0"、recent=[]；完全失败为两者 null；部分记录允许合计，但 window_complete=false 且区块不为 ok，前端标题改为“已获取部分净收付”。最近 3 笔按时间倒序，当前空仓不删除历史收付。

### 6.3 比价与走势

- `live` 使用两腿同次市场响应的正 mark 相除；任一无效为 null。`latest_close` 是已完成 UTC 日线的第一腿收盘/第二腿收盘，不能与 live 混名。
- 30MA 是逐日比价的算术均值，不是两腿均价相除；`vs_ma30_pct=(latest_close/ma30-1)*100`。它以最近收盘比价计算，不以 live 计算。
- 一次获取约 120 个已完成 UTC 日，为 90 天展示提供 29 天均线预热；响应最终保留最近 90 个日历日 points，30/90 切换前端裁剪。均线要求连续 30 个有效共同日期；历史不足只影响相应 MA，不伪造数据。
- 缺日 point 保留日期、缺失价格/比价/MA 为 null，图表断开；全部日线查询失败 points=null。日线过期标 partial，明确展示实际 as_of_date，不把旧值标成今日。缺失最新日时 `vs_ma30_pct=null` 并诊断；旧 MA 可留在历史曲线中，不作为当前信号。
- NBIS/CRWV 这里是 Hyperliquid 永续比价，不是美股现货比价。保持 UTC 日线口径，不能把股票休市自动等同永续停盘；本轮不新增股票交易时段/预言机模式推断。
- 元数据标记下架或缺少合约时显式诊断、不能标行情正常；已存在的可查询持仓仍尽量展示。主网失败不回退测试网。

## 7. 执行顺序与文件范围

这是一轮“多 pair 主网只读看板”交付；以下为实施依赖顺序，不授权额外交易功能。

1. **契约与配置**：先实现两侧配置校验、v2 类型、目录 API、共享 JSON fixtures。先用两个不同模拟地址验证映射，再进入采集与页面。
2. **Rust 采集**：增加 dashboard 子命令、只读 info 客户端、按 DEX/账户过滤、v2 数据计算。运行离线测试，确认旧命令仍为测试网。
3. **Python 服务**：增加目录、pair 快照和公告接口；严格校验、限流并发、缓存隔离、有界子进程输出。用真实 Rust 序列化的固定 fixture 做接口联调，不能只靠手写宽松 mock。
4. **React 页面**：复用组件完成总览、动态导航、详情、ECharts 与空态/错误态。查询 key 与地址切换严格关联。
5. **验证与交付**：完成本节后面的验收，更新使用/部署说明，记录两个仓库提交与兼容/回退办法；实际发布按用户后续部署安排执行。

| 仓库 | 预计文件 |
|---|---|
| hl-trader | `src/main.rs` 新命令；新增 `src/dashboard/{config,client,snapshot}.rs` 或等价模块；必要的纯函数抽取；JSON 配置示例；离线 fixtures/tests；README |
| stock_data 后端 | 新增 `web/services/hyperliquid_pairs_service.py`；扩展 `web/routes/hyperliquid.py`；新增 `tests/test_web/test_hyperliquid_pairs.py` 与 fixtures；原 v1 service 保留 |
| stock_data 前端 | `hyperliquid-page.tsx` 改总览；新增通用详情页和 `components/hyperliquid/`；v2 类型/API；App 路由、sidebar、topbar；复用 `components/charts/echarts-wrapper.tsx` |
| 文档 | 新方案、原方案跳转说明、部署文档主网/测试网分开、目录导航；必要时更新数据来源索引，说明无股票 DB 数据写入 |

不改 `scripts/start-vps.sh`，不改股票市场枚举、股票数据模型或数据库表。当前其他未提交文档与 `.agent-artifacts/` 不是本任务成果，不打包进本次提交。若新增主网客户端可复用 hypersdk 已有只读方法则优先复用；SDK 缺项时使用固定 `/info` DTO，不引入交易 SDK 的主网写入口。

## 8. 验收清单

| 场景 | 必须验证的结果 |
|---|---|
| 两个 pair、两个账户 | A/B 数据分别进入正确卡片/详情，身份字段严格匹配；同 ID 换账户后旧缓存及页面旧数据失效 |
| 同地址用于多 pair | 精确过滤各自合约，资金费不串；不重复相加账户权益或把重叠合约当独立策略收益 |
| 真实多空 | 正/负数量、双向反转、两腿同向、仅一腿、空仓均正确；不依赖固定 long/short 常量 |
| HIP-3 | 请求带正确 DEX，完整 coin 匹配；没有默认原生 DEX 回退；下架状态有提示 |
| 账户模式 | 标准、统一账户、组合保证金、模式查询失败均不误报零权益；可用仓位独立保留 |
| 账户未配置 | null 地址只显示公开行情；账户相关块明确不可用，不调用零地址、不显示空仓 |
| 数值与时间 | 拒绝 NaN/Infinity、负数量、无效日期/地址；0 与 null 分清；分源时间、缓存时间不伪造 |
| 资金费 | 正负净收付、零估算、部分缺费率、完整空历史、分页重复边界/中断、平仓后历史均覆盖 |
| 比价图 | 比价方向稳定；30/90 天；29/30 天均线边界、缺日断线、过期、非正价格、未收盘日剔除 |
| 页面错误态 | items/recent/points 为 null 不崩溃、不显示正常空态；不完整窗口标题正确；切换 B 不闪现 A 数据 |
| 并发与隔离 | 相同请求仅一个子进程，不同 pair 可并发；一个失败不影响另一个；换配置/版本不命中旧缓存 |
| 输入与进程 | 未知 ID 404、路径/URL/命令注入无效；超时/超限进程回收，stdout/stderr 有界；错误不公开本机路径 |
| 只读回归 | mock 断言只请求 info/固定公告来源，无 exchange/签名/转账/状态写入；交易命令仍限定测试网且缺密钥时严格失败 |
| 导航与兼容 | 深链接刷新、侧栏折叠、移动端、深浅主题；股票页面无回归；v1 CLI/接口及原配置仍按原网络工作 |

建议验证命令（以执行时目录及现有环境为准）：

```sh
# /home/vinci/projects/hl-trader
cargo fmt --check
cargo test --offline
cargo clippy --offline --all-targets -- -D warnings
# /home/vinci/projects/stock_data
venv/bin/python -m pytest tests/test_web/test_hyperliquid.py tests/test_web/test_hyperliquid_pairs.py -q
# /home/vinci/projects/stock_data/frontend
VITE_CN_API_URL=/api/cn VITE_US_API_URL=/api/us npm run build
```

默认测试不联网、不读真实私钥、不发送订单。前端构建不替代交互验收，使用固定快照覆盖上述页面状态。真实公开市场 smoke 单独显式运行；拿到用户地址后只读核对两腿仓位与资金费，并记录时间，不用真实订单构造测试。

## 9. 配置、上线与交接

- 提供仓库内无真实地址的配置示例；运行配置只包含公开地址及 pair 定义。管理员为两组分别填主网账户地址，确认是实际主/子账户。
- 新后端配置 `STOCK_HL_TRADER_BIN` 与 `STOCK_HL_PAIRS_CONFIG`；不能靠旧单账户环境变量填补缺项。先部署支持新命令的二进制与新 API，再更新前端；保留 v1 用于回归。
- Web 身份继续与交易身份隔离，不可读取交易私钥或修改交易配置、状态、程序及父目录；v2 不需要交易状态读取权限。公开展示已获用户确认，不额外增加登录系统或账户授权流程。
- 生产构建沿用 `/api/us`。上线前核实当前前端实际托管位置、US 后端运行方式及已存在代理，不照抄旧文档的进程重启命令，不新增端口。
- 回退时恢复上一版前端/API/二进制及其旧配置；旧页面显示原测试网标识，不能用主网缓存伪装旧 v1。回退不触碰交易账本。
- Kimi 交付：两仓库变更列表及提交号、验证结果、桌面/移动端总览与详情截图、配置示例、尚未接入的真实地址项、部署或未部署的明确状态。用户后续若要求交易功能，另出方案，本次只保留稳定 pair ID 与市场身份。

交接摘要：先让不同地址的两组主网仓位可靠地显示在一个多 pair 页面中；行情与账户数据分源失败隔离；只读入口与现有测试网交易入口分开。不要将“页面可见”当作“真实账户已接入”或“生产发布已完成”。
