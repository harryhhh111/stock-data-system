# 美股资讯来源可访问性验证报告

- 验证日期：2026-10-20（本机 UTC，海外服务器）
- 目标窗口：2026-10-13 ~ 10-19（北京时间）
- 方法：curl + python3 实测；每源 ≤10 请求、间隔 ≥2s、带识别 UA
- 说明：仅来源侧验证，不做与参考事件清单的正式命中率对照

---

## 1. Google News RSS

- 接入方式：RSS（无认证），`https://news.google.com/rss/search?q=...&hl=en-US&gl=US&ceid=US:en`
- 本机可用性：HTTP 200，全部 8 个查询成功（公司 3 + 主题 5），无 403/超时
- 内容字段：title、link、guid、pubDate、description（仅原文前一两句的 HTML 片段）、source（媒体名+站点 URL）。**无正文**；link/guid 均为 `news.google.com/rss/articles/...` 跳转包装，非原文直链
- 时间：pubDate 精确到秒，GMT/UTC 时区标注
- 历史窗口：每查询固定返回 100 条，无分页参数。冷主题可回溯数月（HDD+Toshiba 最旧 2026-03-31；Nebius/WDC 最旧 2026-08-24）；热查询被 100 条截断（Micron 最旧仅 10-13，DRAM 最旧 10-14）——**对高频主题，窗口早期条目可能被挤出，需高频抓取落库而非事后回溯**
- 窗口内覆盖：Micron 45 条、WDC 44 条、Nebius 62 条、DRAM 50、HBM 50、CoreWeave 39、"memory chip prices" 30、HDD+Toshiba 6
- 三条行业主线定性观察：
  - CXMT 存储抛售：明显可见（"Micron: CXMT Sparks Fear…"、"Memory Stocks Slide as China's CXMT Flags Denser Chip Design: Micron…Western Digital Drops 2%"、"China's CXMT Swings to Profit After Memory Chip Prices Surge - Bloomberg"）
  - CoreWeave 债务恐慌：明显可见（"How CoreWeave's Debt Binge Brought a Perilous Moment of Reckoning - The Information"、"Nebius Stock Fell 12%…as CoreWeave's Debt Scare Spread - TIKR"、"CoreWeave Falls 4% as HSBC Reiterates Bearish Call on Debt Risk"）
  - 东芝 HDD 扩产：明显可见（"Toshiba to double HDD capacity for AI data centres"、"Toshiba to Double AI Data Center HDD Capacity by FY2027 With $380M Expansion - TrendForce"、"Seagate and Western Digital Stocks Have Tanked on Toshiba Fears - Barron's"）
- 关联方式：纯关键词查询；公司别名需自行维护（"MU" 裸代码噪声大，宜用公司名）；主题查询直接可用
- 成本与限流：免费；8 次请求间隔 3s 未遇限流
- 结论：**可用**。字段齐（除正文）、历史对冷主题足够、热主题需每日落库防截断。

## 2. Finnhub（免费档，company-news）

- 接入方式：REST API + token（`STOCK_FINNHUB_API_KEY` 已在 .env，未打印）
- 本机可用性：HTTP 200，MU/WDC/NBIS 三请求全成功；无效 token 返回 HTTP 401 `{"error":"Invalid API key"}`（错误语义清晰）
- 请求：`company-news?symbol=MU&from=2026-10-13&to=2026-10-19`
- 条目数：MU 248、WDC 97、NBIS 112（窗口 7 天）
- 内容字段：category、datetime（Unix 秒）、headline、id（稳定数字 ID，本次全部唯一）、image、related（代码）、source、summary（1-2 句摘要）、url（`finnhub.io/api/news?id=...` 跳转包装，非原文直链）。无正文
- 时间：秒级精度，UTC；from/to 按日期过滤
- 窗口覆盖：WDC 最旧 10-13 00:57 UTC、NBIS 最旧 10-13 04:45 UTC，覆盖窗口起点；MU 最旧 10-14 09:51 UTC（10-13 无条目或热点截断，248 条未见明确截断提示）
- 关联方式：代码直连（symbol 参数），related 字段回显代码；不支持主题/行业查询（主题需走 Google News 等）
- 成本与限流：30 次/秒是叠加在套餐限额之上的每秒上限，不替代套餐额度（免费档另有套餐级配额）；本次低频试采无压力，持续额度未验证
- 结论：**可用**。代码直连、字段规范、稳定 ID 适合去重落库；缺点是 url 为包装链、无主题维度。

## 3. TrendForce（新闻中心）

- 接入方式：网页（无认证），服务端渲染，curl 可直接采；RSS 不存在（`/presscenter/rss` 返回 404 软页）
- 本机可用性：HTTP 200（列表、详情均成功）
- 列表页：`https://www.trendforce.com/presscenter/news?page=N`，含分类（Semiconductors/Display/LED 等）、标题、日期（如 "19 October 2026"）；详情 URL 形如 `/presscenter/news/20261019-13275.html`（日期+数字 ID，天然稳定 ID）
- 详情页：HTTP 200，含日期、正文（服务端 HTML），canonical URL 稳定；`<h1>` 是站点通用头，标题需从正文区块提取
- 历史窗口：`?page=N` 分页存在（至少 4 页可见链接），可回溯；未逐页验证深度
- 时间：日期级精度（无时分秒），页面未显式标时区（台湾站，默认 CST 口径需注意）
- 主题覆盖：存储价格/DRAM/NAND 为核心议题；Google News 结果中已见其 Toshiba HDD 扩产报道被收录，说明窗口内相关主题有产出
- 成本与限流：免费公开，3 次请求无拦截
- 结论：**可用（网页抓取）**。字段够用、URL 稳定，但日期级精度、需自解析 HTML、无 API。

## 4. Blocks & Files（存储行业站）

- 接入方式：理论 RSS/网页；**实测被反爬拦截**
- 失败证据：
  - `https://blocksandfiles.com/` → HTTP 200 但返回 2101 字节的 "Are we human?" 挑战页（Labrador PoW，JS 计算 proof-of-work 后调 `/_labrador/pow/v0/verify` 再 reload）
  - `/feed` → 301 到 `https://www.blocksandfiles.com/feed` → 同样 2101 字节挑战页
  - `/sitemap_index.xml` → 301（未再追，同域预计同样拦截）
- 结论：**不可用（curl 不可采）**。全站（含 RSS）有 JS proof-of-work 反爬，需真实浏览器/渲染方案或人工通过挑战后带 cookie，不适合自动化日采。

## 5. Data Center Dynamics（DCD，NBIS/AI 云主题）

- 接入方式：RSS（`/en/rss/`，无认证）+ 服务端渲染网页
- 本机可用性：HTTP 200；`/rss.xml` 是 404，正确路径从首页 `<link rel="alternate">` 发现（`/en/rss/`、`/en/atom/`）
- RSS 字段：title、link（原文直链，非包装）、pubDate（秒级，+0000）、description、guid、enclosure；**仅 20 条，最旧 2026-10-19 16:10 UTC——窗口极短，只够当日用，无法回溯 10-13**
- 网页：首页服务端渲染，含 `/en/news/<slug>/` 文章链接（slug 可作文本 ID）和 ISO 日期（2026-10-15 等），可抓列表页补历史（未逐页验证分页深度）
- 关联方式：主题关键词（无代码概念）
- 结论：**受限可用**。源本身可采且直链友好，但 RSS 只有 ~1 天窗口；覆盖窗口需每日落库或抓网页列表。

## 6. Alpha Vantage NEWS_SENTIMENT

- 接入方式：REST API + key（免费 25 req/day）
- 实测：`function=NEWS_SENTIMENT&tickers=MU&apikey=demo` → HTTP 200 但返回 `{"Information": "The **demo** API key is for demo purposes only..."}`，即 demo key 不支持此接口，拿不到数据形态
- .env 中无 Alpha Vantage key（grep 确认）
- 结论：**需要 key，未测**。接口可达，但无有效 key，无法评估字段与限流实际表现。

---

## 汇总

| 源 | 结论 | 一句话理由 |
|---|---|---|
| Google News RSS | 可用 | 公司+主题查询全通、秒级 UTC、窗口内条目充足；热主题 100 条截断需每日落库 |
| Finnhub company-news | 可用 | 代码直连、字段规范、稳定 ID、窗口覆盖好；url 为包装链、无主题维度 |
| TrendForce | 可用（网页） | 服务端渲染可 curl、URL 含日期+ID；仅日期级精度、需解析 HTML |
| Blocks & Files | 不可用 | 全站 JS PoW 反爬（含 RSS），curl 不可采 |
| DCD | 受限可用 | 可采且直链，但 RSS 仅 ~20 条/1 天窗口，需每日落库或抓网页列表 |
| Alpha Vantage | 未测（缺 key） | demo key 不支持 NEWS_SENTIMENT，.env 无 key |

行业主线定性：CXMT 抛售、CoreWeave 债务、东芝 HDD 扩产三条线在 Google News 窗口内均明显可见；Finnhub 的公司新闻（NBIS 条）也含 CoreWeave 债务恐慌传导内容。
