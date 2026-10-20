# 来源覆盖对照报告 v3.1（收紧修正版，对照基准 v2，2026-10-20）

> 本版相对 v3 的三处修正：① W-N1（SeekingAlpha）与 W-N3（Reuters）的 guid 错配/抄写错误已按快照程序化重取修正，**全部 RSS 证据 guid 改为脚本从快照提取**，杜绝手工转录；② E1 窗口外证据（Yahoo，北京 10-12 15:01）移入背景证据区，命中依据仅保留窗口内条目；③ N-P2~N-P5 发布时间核验**走路径 (a)**：4 篇 blog 原文 HTML 已存档至 `evidence/watchlist_daily_news/`，页面 JSON-LD 含 `datePublished`/`dateModified` 结构化时间元数据，与 RSS pubDate 一致——不再依赖 X 帖旁证。证据引用全部改为随文档归档的相对路径。

## 0. 基准、批次与复算口径

- 基准：[参考事件基准表 v2](WATCHLIST_DAILY_NEWS_BASELINE_V2.md)（2026-10-21 终审通过），分母 = 15。
- **支撑矩阵的唯一批次 = 批次 B**，快照存于 `evidence/watchlist_daily_news/`（清单见 `evidence/watchlist_daily_news/BATCH_MANIFEST.md`）：
  - Finnhub：`fh_MU.json`（248 总/209 窗口内）、`fh_WDC.json`（121/100）、`fh_NBIS.json`（129/111）；请求 `company-news?symbol=X&from=2026-10-12&to=2026-10-20`，2026-10-20 22:26（北京）抓取。
  - Google News RSS：`gn_0.xml`（"Micron Technology"，100/83）、`gn_1.xml`（"Western Digital"，100/60）、`gn_2.xml`（"Nebius"，100/75）、`gn_3.xml`（"CXMT DRAM"，100/76）、`gn_4.xml`（"CoreWeave bond"，100/90）、`gn_5.xml`（"Toshiba HDD"，53/14）；均带 `when:14d`，2026-10-20 22:28（北京）抓取。
- **批次对账**：可访问性报告为更早的批次 A（21:53，Finnhub `from=2026-10-13&to=2026-10-19`：MU 248/WDC 97/NBIS 112；RSS 8 查询：Micron 45/WDC 44/Nebius 62 等）。差异解释：① from/to 日期范围不同（B 批更宽 → WDC/NBIS 条数更多）；② 查询词不同（A 批用 `Micron OR "MU"`、裸名，B 批用公司全名+when:14d）；③ 抓取时间差约 35 分钟；④ 窗口计数口径不同（A 批按 UTC 日期 10-13~10-19，B 批按北京窗口 [10-13 00:00, 10-20 00:00)）。**最终矩阵以批次 B 为准**。MU 两批同为 248 条且 B 批最旧仅 10-14 15:01 UTC（WDC/NBIS 均达 10-12）——疑似接口对高热代码存在条数上限，**Finnhub MU feed 窗口前段（北京 10-13 00:00~10-14 23:01）无条目留存，属"历史覆盖不够"实证**（不影响本次矩阵命中，因各 MU 事件命中条目均在 10-15 之后）。
- **Finnhub 请求日期语义**：from/to 是 **UTC 日期**。北京窗口 [10-13 00:00, 10-20 00:00) 对应 UTC [10-12 16:00, 10-19 16:00)；批次 A 的 `from=2026-10-13` 不覆盖窗口起点，批次 B 的 `from=2026-10-12&to=2026-10-20` 完整覆盖后再按秒级 datetime 过滤，矩阵以 B 批参数为准。

## 1. v2 全量对照矩阵 + 条目级证据索引

记号：✅=确定命中；❌=未命中（分类见 §3）。证据引用格式：`evidence/watchlist_daily_news/<文件>#guid-…<末24位>` 或 `evidence/watchlist_daily_news/<文件>#id=<Finnhub id>`，均可直接在归档快照中检索定位。时间为条目原始戳（GMT）换算北京（+8h）。

### 1.1 Finnhub 证据（15 事件）

| 事件 | 结论 | 条目级证据（`evidence/watchlist_daily_news/fh_*.json#id=`，时间北京） |
|---|---|---|
| M-P1 | ✅/✅ | `fh_MU.json#id=143006624`（10-19 20:00，Yahoo，"Micron to Host Technology Webinar on Oct. 21"，与 GlobeNewswire 稿头 ET 08:00 精确一致） |
| M-N1 | ✅/✅ | `fh_MU.json#id=142945570`（10-16 20:48，Barron's 经 Yahoo 分发 "…'Buy the Dip' Calls"）；`fh_MU.json#id=142950041`（10-17 03:42，"Micron stock tumbles…"） |
| W-N1 | ✅/✅ | `fh_WDC.json#id=142949194`（10-16 23:52，Barron's 东芝文）；`fh_WDC.json#id=142902126`（10-15 11:11，SeekingAlpha "Toshiba Selloff Changes Little"） |
| N-N1 | ✅/✅ | `fh_NBIS.json#id=142962705`（10-18 01:34，"Nebius Stock Fell 12%…Debt Scare Spread"）；`fh_NBIS.json#id=142949993`（10-17 03:37，"CoreWeave, Nebius, and Neoclouds Sink"） |
| E1 | ✅/✅ | `fh_MU.json#id=142885140`（10-14 23:03，"Rosenblatt Calls CXMT-Driven Sell-Off Overblown"）；`fh_MU.json#id=142921372`（10-16 03:33，"DRAM Giants Get Rich as China Builds a Threat"）；`fh_WDC.json#id=142887421`（10-15 02:01，"SK Hynix Sinks 5% as China's CXMT…; Western Digital Drops 4%"） |
| E2 | ✅/✅ | `fh_NBIS.json#id=142882527`（10-14 21:33，"CoreWeave Falls 10% as Bond Yield Touches 15%…Nebius Drops 6%"）；`fh_NBIS.json#id=142920725`（10-16 01:47，"Nebius stock breaks below $200…"） |
| N-P2~N-P5 | ❌/— | fh_NBIS 窗口内 111 条逐条扫描无任何 nebius.com/blog 条目（确实无命中） |
| W-N3 | ✅/✅ | `fh_WDC.json#id=142915597`（10-15 21:05，"Supermicro Appoints Mark Long as CFO"）；`fh_WDC.json#id=142950910`（10-17 04:15，"WDC Says CFO Mark Long Is Leaving"） |
| M-N2 | ✅/✅ | `fh_MU.json#id=142964304`（10-18 01:30，"Why Micron Is Betting $35 Billion on Its Own Stock"） |
| M-N3 | ✅/✅ | `fh_MU.json#id=142997610`（10-19 15:46，"Union Voted 99% to Authorize a Strike"）；`fh_MU.json#id=142920340`（10-17 01:17，"Workers Want 15% of Profits"） |
| N-N2 | ✅/✅ | `fh_NBIS.json#id=142886775`（10-14 23:24，"Rosenblatt initiates Nebius at Buy with $304 target"）；`fh_NBIS.json#id=142946173`（10-16 18:15，"Rosenblatt places $304 price target"） |
| E5 | ✅/✅ | `fh_NBIS.json#id=142886742`（10-15 01:06，"Why Gartner AI Outlook Sent CoreWeave Stock…"）；`fh_NBIS.json#id=142917479`（10-15 17:27，Benzinga "Gartner Warns 80%…by 2030"） |

### 1.2 Google News RSS 证据（15 事件；guid 全部脚本提取自快照）

| 证据行 | 引用 | pubDate (GMT) | 来源 | 标题（截断） | 备注 |
|---|---|---|---|---|---|
| M-P1/RSS | `evidence/watchlist_daily_news/gn_0.xml#guid-…ZmLTNpNTc2ZGF4NUlJRmVEUQ` | 10-19 12:00:00 | Yahoo Finance | Micron to Host Technology Webinar on Oct. 21 | 主证据 |
| M-N1/RSS-1 | `evidence/watchlist_daily_news/gn_0.xml#guid-…Vta0oxamFodDhQOVZ0RGt6QQ` | 10-12 16:32:00 | The Motley Fool | Why Micron Stock Dropped on Monday | 主证据（北京 10-13 00:32，窗口内） |
| M-N1/RSS-2 | `evidence/watchlist_daily_news/gn_0.xml#guid-…eThJdnRGMmk3OV96UkYtSDRJ` | 10-16 19:42:25 | Yahoo Finance | Micron stock tumbles despite…overblown | 主证据 |
| W-N1/RSS-1 | `evidence/watchlist_daily_news/gn_1.xml#guid-…FMRzhxZzl4RmRpZ3JiSDFydw` | 10-16 15:52:00 | Barron's | Seagate and Western Digital Stocks Have Tanked on Toshiba Fears | 主证据 |
| W-N1/RSS-2 | `evidence/watchlist_daily_news/gn_1.xml#guid-…wUlprSVREbXVpWXpMeElWaHc` | 10-15 07:11:21 | Seeking Alpha | Western Digital Over Seagate: Toshiba Selloff Changes Little | 主证据（**本轮修正 guid 错配**：v3 所引 guid 实际指向 10-07 Yahoo 文章，已按快照重取） |
| N-N1/RSS-1 | `evidence/watchlist_daily_news/gn_2.xml#guid-…RweTgtTXI2b21oN0I1WmY4Wg` | 10-15 16:01:19 | CNBC | What's behind the sell-off in AI neocloud stocks… | 主证据 |
| N-N1/RSS-2 | `evidence/watchlist_daily_news/gn_2.xml#guid-…OGNtaGVpc28wX1VPMEg3dXhz` | 10-14 21:32:06 | Seeking Alpha | Nebius Group declines 10% amid tech sell-off | 主证据 |
| E1/RSS-主 | `evidence/watchlist_daily_news/gn_3.xml#guid-…4RXREcUM2OEN1dnRicThBN3o` | 10-13 11:00:00 | Tom's Hardware | CXMT president says company's first 4F² DRAM… | **主证据（窗口内）** |
| E1/RSS-2 | `evidence/watchlist_daily_news/gn_3.xml#guid-…dPSHVsTm1MVnJ1Z1JYcUprZw` | 10-13 08:24:49 | digitimes | CXMT's server DRAM push raises margin concerns | 主证据（窗口内） |
| E1/RSS-3 | `evidence/watchlist_daily_news/gn_3.xml#guid-…qenJnMk9XY3c3WURWU0dieWc` | 10-14 08:32:00 | Nikkei Asia | Memory breakthrough by China's CXMT puts chip rivals on back foot | 主证据（窗口内） |
| E2/RSS-1 | `evidence/watchlist_daily_news/gn_4.xml#guid-…9UV3paVU9MalVpa0FFbzRBZw` | 10-16 10:03:00 | Reuters | CoreWeave bond rout tests market's math on AI buildout | 主证据 |
| E2/RSS-2 | `evidence/watchlist_daily_news/gn_4.xml#guid-…BmSEwzZ19ZbzdhaGZWRVpqQQ` | 10-14 16:24:51 | CNBC | CoreWeave shares tumble as bond yields near 15% | 主证据 |
| E2/RSS-3 | `evidence/watchlist_daily_news/gn_4.xml#guid-…NGQlRGNkxJcjJfLWM1YVhYMQ` | 10-14 18:29:27 | Bloomberg.com | CoreWeave's Bonds Turn Distressed | 主证据 |
| N-P2/RSS | `evidence/watchlist_daily_news/gn_2.xml#guid-…NjN0oyQmtCRDc0RVZQV056cA` | 10-13 14:32:07 | Nebius | Open-source NVIDIA Run:ai…Nebius AI Cloud Applications | 内容命中；**时间经 `evidence/watchlist_daily_news/nebius_blog_runai.html` 的 datePublished 2026-10-13（dateModified 16:04Z）独立核验** |
| N-P3/RSS | `evidence/watchlist_daily_news/gn_2.xml#guid-…RqX2QtWVZjU0xEMVFqV19DTg` | 10-16 13:35:20 | Nebius | Nebius Academy and NVIDIA DLI… | 同上：`nebius_blog_dli.html` datePublished 2026-10-16（modified 13:00Z） |
| N-P4/RSS | `evidence/watchlist_daily_news/gn_2.xml#guid-…1MVVA5QTBrZTRwbmdsUTEteQ` | 10-16 20:00:56 | Nebius | Three new open-weight models… | 同上：`nebius_blog_models.html` datePublished 2026-10-16（modified 14:42Z） |
| N-P5/RSS | `evidence/watchlist_daily_news/gn_2.xml#guid-…5SzVlc1BJMzAxQVJlcGtQS2c` | 10-19 07:37:55 | Nebius | …AI Builder Program in Europe | 同上：`nebius_blog_builder.html` datePublished 2026-10-19（modified 09:39Z） |
| W-N3/RSS-1 | `evidence/watchlist_daily_news/gn_1.xml#guid-…LXg1RDllZExOT2F4blRJdFU4` | 10-15 14:37:12 | Reuters | Super Micro names former Western Digital executive Mark Long as new CFO | 主证据（**本轮修正 guid 抄写错误**，已按快照重取） |
| W-N3/RSS-2 | `evidence/watchlist_daily_news/gn_1.xml#guid-…VpOUVaZDNtMDdrSXVtMlZ0VQ` | 10-16 20:15:33 | Yahoo Finance | Western Digital (WDC) Says CFO Mark Long Is Leaving | 主证据 |
| M-N2/RSS | `evidence/watchlist_daily_news/gn_0.xml#guid-…WF9vd2owcEQtUnpxa0c4ZzdR` | 10-13 21:37:00 | The Motley Fool | Micron Just Raised Its Buyback Authorization to $35 Billion | 主证据 |
| M-N3/RSS-1 | `evidence/watchlist_daily_news/gn_0.xml#guid-…HejRHZGs3S0JSZjhQMVRzMjA` | 10-15 07:21:07 | Reuters | Taiwan president calls for Micron, workers to negotiate | 主证据 |
| M-N3/RSS-2 | `evidence/watchlist_daily_news/gn_0.xml#guid-…hTUWw3M3dfUUlnaXhIWTZjUQ` | 10-19 08:26:00 | The Motley Fool | A Micron Union in Taiwan Voted 99% to Authorize a Strike | 主证据 |
| N-N2/RSS | `evidence/watchlist_daily_news/gn_2.xml#guid-…lVy1uVmQzWUJRbTVOMkZ2VHc` | 10-14 15:24:33 | Yahoo Finance | Rosenblatt initiates Nebius at Buy with $304 target | 主证据 |
| E5/RSS-1 | `evidence/watchlist_daily_news/gn_2.xml#guid-…S1NxbmU3Z1hZcHktNXVNeGdv` | 10-15 13:34:00 | 24/7 Wall St. | AI Cloud Stocks Sink as Gartner Warns Most Neoclouds… | 主证据 |
| E5/RSS-2 | `evidence/watchlist_daily_news/gn_4.xml#guid-…aajhxa09yVG4yYTV0Q19BbGc` | 10-14 22:28:00 | The Motley Fool | Stock Market Today, Oct. 14: …Gartner Neocloud Warning | 主证据（主题查询） |

### 1.3 背景证据区（窗口外或仅作背景，不作为命中依据）

| 引用 | pubDate (GMT) | 说明 |
|---|---|---|
| `evidence/watchlist_daily_news/gn_3.xml#guid-…YW01WW9OYmM4MVo0MHNhOFdz` | 10-12 07:01:49 | Yahoo "Micron, SK Hynix Stocks Slip Overnight: China's CXMT…"——**北京 10-12 15:01，窗口外**，v3 曾误列为 E1 命中证据，本版移出；E1 命中由 §1.2 三条窗口内证据支撑，不受推翻 |
| `evidence/watchlist_daily_news/gn_3.xml` 中 10-11~10-12 的 SCMP/Global Times/TweakTown 等 CXMT 条目 | 10-11~10-12 | E1 事件的窗口前早期报道背景（部分呈 07:00:00 GMT 批处理特征） |
| `evidence/watchlist_daily_news/gn_1.xml`、`gn_5.xml` 中 10-07~10-09 的东芝/TDK 条目 | 10-07~10-09 | W-N1 触发事件（10-02 东芝扩产）的窗口前报道背景（多呈批处理时间戳） |

## 2. 汇总数（确定命中 vs 候选/时间待定命中）

| 源 | 确定命中（内容/关联） | 候选/时间待定 | 未命中 |
|---|---|---|---|
| **Google News RSS** | **15/15，15/15** | 0 | 0 |
| **Finnhub** | **11/15，11/11** | 0 | 4（N-P2~N-P5，确实无命中） |
| **TrendForce** | 0/1（仅 E1 适用） | 0 | E1（确实无命中，新闻中心入口） |

说明：blog 四篇经路径 (a) 完成独立时间核验（blog HTML 内 JSON-LD `datePublished` 均在窗口内：10-13/10-16/10-16/10-19，与 RSS pubDate 同日），**无需降级，确定命中维持 15/15、时间待定 0**。v3 提到的"X 帖旁证"无留存材料，本版证据链不引用任何 X 帖。

可疑时间戳扫描（整 07:00:00 GMT 批处理特征）：Micron 7/100、WDC 28/100、Nebius 5/100、CXMT 7/100、CoreWeave 1/100、Toshiba HDD 19/53——逐条核对后**无一被用作命中证据**（均在窗口外或已剔除/仅作背景）。

## 3. 阴性分类

- **Finnhub × N-P2~N-P5 = 确实无命中**：fh_NBIS 窗口内 111 条无 blog 条目。措辞：**"本次 NBIS 样本未命中四篇官方 blog，不能单独承担该入口覆盖"**。
- **TrendForce × E1 = 确实无命中（新闻中心入口）**：窗口内 4 篇新闻稿（10-13 CPO、10-14 CSP CapEx/AI 服务器、10-15 AR 眼镜、10-19 光纤）无一为 CXMT/DRAM 主题（§5）。
- **Reuters 正文 = 权限不足**：reuters.com 未打开、U.S. News 转载 HTTP 403；命中判断基于 RSS 标题+其他可开来源。
- **历史覆盖不够（实证）**：Finnhub MU feed 最旧 10-14 15:01 UTC（两批同 248 条，疑接口上限）；Google RSS 5/6 查询触 100 条上限（CoreWeave bond 最旧 北京 10-13 19:49，首日早段可能截断，但 E2 起点 ET 10-14，影响≈0）。

## 4. 相关性 / 重复抽查（批次 B，每源每公司 20 条窗口内，等距抽样）

| 源·公司 | 样本相关率 | 重复率（规范化标题精确去重，全量） | 说明 |
|---|---|---|---|
| Finnhub MU | 13/20（65%） | 0 / 209 | 无关条为大盘综述/宏观/其他公司快讯 |
| Finnhub WDC | 12/20（60%） | 0 / 100 | 同上 |
| Finnhub NBIS | 14/20（70%） | 0 / 111 | 同上 |
| RSS Micron | 20/20（100%） | 6 / 83 | 重复为同稿多源分发（Yahoo/AOL/TradingView 同题） |
| RSS WDC | 20/20（100%） | 7 / 60 | 同上 |
| RSS Nebius | 20/20（100%） | 7 / 75 | 同上 |

注：相关性为关键词规则（偏保守）；Finnhub 重复率 0 指 feed 内无同题重复，不代表与 RSS 之间无重叠。

## 5. TrendForce 入口级证据（矛盾消除，维持 v3 结论）

v2 覆盖报告"动态加载不可回溯"系 FetchURL 抽取不全之误，以 curl 实证为准：

| 入口 | 实测（证据文件） | 结论 |
|---|---|---|
| 新闻中心列表页 | HTTP 200 静态 HTML（`evidence/watchlist_daily_news/tf_list.html`），5 篇：10-19 光纤、10-15 AR 眼镜、10-14 CSP CapEx/AI 服务器、10-13 CPO、10-08 笔记本 | 可 curl 回溯；窗口内 4 篇无 CXMT/DRAM 稿 → E1 确实无命中 |
| 分页 `?page=N` | `?page=2` → HTTP 200（`evidence/watchlist_daily_news/tf_list_p3.html`），得 09-22~10-08 五篇（含 4Q26 价格稿 20260930-13258）；分页链接 page=1..4、838 | 分页有效、归档 838 页；`?page=1` 返回同首页（怪癖已记录） |
| 详情页 | HTTP 200（`evidence/watchlist_daily_news/tf_detail.html`），标题与正文关键词齐全 | 静态可抓，URL 含日期+数字 ID |
| 站内搜索 | 返回付费报告库（非新闻流） | 不可用于新闻检索 |

结论：TrendForce 新闻中心可作为行业价格/供需类来源接入；本次窗口内无 CXMT 稿（E1 未命中为"确实无"）。

## 6. 结论（按用户裁定措辞）

1. **"Google 主召回，Finnhub 提供证券标签、稳定 ID 和交叉核对"**——Finnhub 对 Google 独有的 blog 新增事件覆盖为 0，双向互补未证明。
2. Finnhub 与 blog 入口：**"本次 NBIS 样本未命中四篇官方 blog，不能单独承担该入口覆盖。"**
3. 空集案例：**"所列空集案例未发现冲突"**（相关性/重复抽查见 §4）。
4. 成本：**"本次试采未发生新增接口费用，具备进入接入验证的条件"**——持续额度、时效、稳定性未验证。
5. Finnhub 限流：**30 次/秒是叠加在套餐限额之上的每秒上限，不替代套餐额度**。
6. 覆盖率：确定命中 RSS 15/15、Finnhub 11/15（候选/时间待定 0）；全部命中落实为 §1 条目级证据，快照与 blog 原文存 `evidence/watchlist_daily_news/`。
