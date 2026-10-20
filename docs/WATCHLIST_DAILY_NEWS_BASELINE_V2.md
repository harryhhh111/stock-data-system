# 参考事件基准表 v2（2026-10-20 冻结，2026-10-21 用户终审通过并归档）

## 版本说明（v1 冻结版 → v2）

v1（2026-10-20 冻结，口径 A，分母 = 6）内容不改写；v2 在其上新增，差异如下：

1. **采集口径修订：公司官方 blog 纳入"公司发布"，与 newsroom 分列两个独立入口**。依据：首轮对照发现 Nebius 官方 blog（nebius.com/blog）窗口内有 4 篇自发内容而 newsroom 为空——两个入口更新节奏不同，必须分开监控。空集案例 N-P0 相应收窄为"newsroom 新闻稿窗口内为空"（仍成立），blog 频道内容另立事件（N-P2~N-P5）。
2. **新增 9 个事件，分母 6 → 15**：
   - N-P2~N-P5：Nebius blog 4 篇（逐篇已打开核实，见下）。
   - W-N3：WDC CFO Mark Long 离职转投 SMCI（SMCI 官方 PR + Reuters + WDC 侧报道三方核实；EDGAR 确认 WDC 截至 10-20 无对应 8-K）。
   - M-N2：MU 回购授权升至 $35.16B（董事会行动 10-08 窗口外，窗口内多家主流财经媒体持续报道，按口径 A 计入）。
   - M-N3：MU 台湾工会罢工授权投票（投票 ~10-09/10 窗口外，窗口内 Reuters 报道台当局介入、Fool 等持续报道，按口径 A 计入）。
   - N-N2：Rosenblatt 首覆 NBIS Buy/$304（窗口内 10-14 多家报道 $304 目标价；首覆日期有 10-08/10-14 两种信号，见备注）。
   - E5：Gartner 警告 80% neocloud 2030 前无法独立生存（ET 10-14，E2 恐慌催化剂之一，行业事件）。
3. **评估后未纳入**：Netlist-Micron $600M 和解（本体 10-06 窗口外；窗口内仅律所通稿 Sterne Kessler 10-15 与评论，主流财经媒体无窗口内新增事实报道）；MU $50M 美国存储研究承诺（首轮观察项，仅单源 Yahoo 10-17，未找到第二来源，证据不足）。
4. 沿用口径 A：窗口内发布的相关资讯计入，窗口外事件的持续报道计入，原始事件时间单列；可抓取性单独评价；因果链互链不重复计数。

## 一、事件区（v2 全量，分母 = 15）

### v1 既有 6 事件（结论不变，详见 v1 小节，此处保留索引行）

| 事件 ID | 纳入时间（北京） | 原始事件时间 | 关联公司 | 全文可抓取性 | 事件关系 |
|---|---|---|---|---|---|
| M-P1 | 北京 10-19 20:00（GlobeNewswire 稿头 ET 08:00 已核实） | 同左 | MU | 可抓取（双源已打开） | 独立 |
| M-N1 | MU 收盘下跌 北京 10-13/10-14/10-15/10-17 04:00（ET 收盘次日） | 诱因公告 北京 10-10（见 E1） | MU | 部分可抓取（行情可 curl；Barron's 付费墙） | E1→M-N1 |
| W-N1（合并 E3） | WDC 收盘下跌 北京 10-15/10-17 04:00；Barron's 10-16 | 触发 10-02（Nikkei/Tom's 东芝扩产） | WDC | 部分可抓取 | E3 已并入；争议行情不作匹配条件 |
| N-N1 | 北京 10-14 22:13 盘中锚定；收盘 北京 10-15/10-17 04:00 | 诱因在窗口内（E2） | NBIS | 可抓取（财联社/Dombey/CNBC 已打开） | E2→N-N1 |
| E1 | 公告 北京 10-10；窗口内 4+ 篇英文报道 10-12~10-14 | **北京 10-10**（持续报道计入） | MU、WDC（行业） | 可抓取（一财+4 英文源已打开） | E1→M-N1 |
| E2 | 起点 ET 10-14；盘中 北京 10-14 22:13；延续至北京 10-17 凌晨 | 窗口内 | NBIS（传导） | 部分可抓取（CNBC 已打开；Reuters 403） | E2→N-N1；E5 为催化剂之一 |

### v2 新增 9 事件

| 事件 ID | 纳入时间（北京）及依据 | 原始事件时间 | 关联公司 | 证据位置 | 全文可抓取性 | 事件关系 |
|---|---|---|---|---|---|---|
| N-P2 | 北京 10-13 22:32（Google News RSS 时间戳；blog 页未显示时刻，日期级） | 同左 | NBIS | [Nebius blog：Open-source NVIDIA Run:ai available in Nebius Applications](https://nebius.com/blog/posts/nvidia-run-ai-open-source-nebius-applications)【已打开全文】 | **可抓取** | 公司发布·blog 入口（首发合作：NVIDIA Run:ai 开源，Nebius 为 launch partner） |
| N-P3 | 北京 10-16 21:35（RSS 时间戳） | 同左 | NBIS | [Nebius blog：Nebius Academy and NVIDIA DLI bring hands-on AI courses](https://nebius.com/blog/posts/nebius-academy-nvidia-dli-hands-on-ai-courses)【已打开全文，标题为 "expand hands-on AI training"】 | **可抓取** | 公司发布·blog 入口（GTC Berlin 教育合作、DLI 课程上架 Nebius Academy） |
| N-P4 | 北京 10-17 04:00（RSS 时间戳 10-16 20:00 GMT；X 编辑记录 10-16 16:20 ET 旁证） | 同左 | NBIS | [Nebius blog：Three new open-weight models from US companies give builders more choice](https://nebius.com/blog/posts/three-new-open-weight-models-from-us-companies-give-builders-more-choice)【已打开全文】 | **可抓取** | 公司发布·blog 入口（Reflection Beam / Meta Muse Spark Open / NVIDIA Nemotron 3.5 Super VL 三款模型 Day-0 上线 Token Factory，含定价） |
| N-P5 | 北京 10-19 15:37（RSS 时间戳 10-19 07:37 GMT） | 同左 | NBIS | [Nebius blog：NVIDIA and Nebius expand the AI Builder Program in Europe](https://nebius.com/blog/posts/ai-builder-program-in-europe)【已打开全文】 | **可抓取** | 公司发布·blog 入口（$10M 欧洲扩展，联合 Accel/NVIDIA；与行业线 AI 算力生态相关） |
| W-N3 | SMCI 任命 PR：ET 10-15（Reuters 北京 10-15 22:37）；WDC 确认离职：ET 10-16 16:15 = 北京 10-17 04:15（Zacks/Yahoo） | 离职生效 ~ET 10-14（SMCI 8-K 口径） | WDC | [Supermicro 官方 PR](https://www.supermicro.com/en/pressreleases/supermicro-appoints-mark-long-chief-financial-officer)【已打开全文：10-15，"effective immediately"，Long 前任 WDC CFO&CSO】；[Reuters 10-15](https://www.reuters.com/technology/super-micro-appoints-mark-long-chief-financial-officer-2026-10-15/)【未打开】；[Fortune 10-15 背景报道](https://fortune.com/2026/10/15/supermicro-new-cfo-mark-long-western-digital-nvidia-smuggling/)【未打开】；EDGAR WDC submissions 10-20 复核：无对应 8-K（申报期限 4 个交易日未满） | **可抓取**（SMCI 官方 PR 可 curl） | 独立（公司管理层变动）；WDC 侧暂无官方稿件 |
| M-N2 | 窗口内报道：Yahoo "Micron board raises buyback ceiling to $35.16bn"（10-12，日期级）；Fool 北京 10-14 05:37；GuruFocus 10-14；Barchart 10-17/18 | **董事会行动 ET 10-08**（窗口外，持续报道计入） | MU | [Fool 10-13/14](https://www.fool.com/investing/2026/10/13/micron-just-raised-its-buyback-authorization-to-usd35-billion-prediction-it-spends-more-buying-back-stock-in-the-next-year-than-in-the-past-8-combined/)【未打开，标题/日期多源确认：授权 $35.16B、12-09 方可启动（CHIPS 限制）】；[Barchart 10-18](https://www.barchart.com/story/news/8143258/why-micron-is-betting-35-billion-on-its-own-stock)【未打开】 | 部分可抓取（标题级多源，原文未打开） | 独立（公司资本配置） |
| M-N3 | 窗口内报道：Reuters "Taiwan president calls for Micron, workers to negotiate"（北京 10-15 15:21）；Fool "Union Voted 99% to Authorize a Strike"（北京 10-19） | **投票 ~10-09/10**（Barchart 10-10 已报道授权；窗口外，持续报道计入） | MU | Reuters（RSS 标题已核，原文未打开）；Fool（未打开）；Finnhub MU feed 另见 10-17 "Workers Want 15% of Profits"（未打开） | 部分可抓取（标题级多源，原文未打开） | 独立（劳工事件）；注：10-20 工会宣布一日罢工（窗口外后续，不计入） |
| N-N2 | ET 10-14 11:24 = 北京 10-14 23:24（Finnhub/Yahoo "Rosenblatt initiates Nebius at Buy with $304 target"）；Blockspace 北京 10-15 00:44 | 首覆日期存两说：RSS 见 10-08 "Rosenblatt Initiates at Buy"（pubDate 有批处理痕迹）与 10-12 "New Buy Rating" 提及；$304 目标价报道集中在窗口内 10-14，按口径 A 计入 | NBIS | Yahoo/Finnhub（未打开原文）；Blockspace（未打开）；[24/7 Wall St 10-16 "Rosenblatt places $304 price target"](https://247wallst.com/)【未打开】 | 部分可抓取（标题级多源） | 独立（分析师评级） |
| E5 | ET 10-14（Finnhub 首条 北京 10-15 01:06 "Why Gartner AI Outlook Sent CoreWeave Stock, Neoclouds Reeling"）；密集报道 ET 10-14~15 | 窗口内（Gartner 报告发布日期级，未精确） | NBIS（传导）、行业 | [24/7 Wall St：AI Cloud Stocks Sink as Gartner Warns Most Neoclouds Won't Survive Independently](https://247wallst.com/)（北京 10-15 21:34）【未打开】；Benzinga "Gartner Warns 80%...by 2030"（10-15 17:27）【未打开】；Yahoo "AI Race Puts $3.5T at Write-Down Risk"（10-15 16:36）【未打开】；Fool "Stock Market Today, Oct. 14: ...Gartner Neocloud Warning"（北京 10-15 06:28）【未打开】 | 部分可抓取（标题级多源，原文未打开） | E5→E2（催化剂）；与 E2 互链不重复计数 |

**汇总行：事件区 15 行（v1 六行 + v2 新增九行），全部计入分母，分母 = 15（v2，2026-10-20 冻结，2026-10-21 终审通过）。**

## 二、空集案例区（校验样本，不计入分母）

| 事件 ID | 结论 | 依据与访问状态 | 标注 |
|---|---|---|---|
| M-S0 | MU 窗口内无 8-K/10-K/10-Q（及 /A）申报 | data.sec.gov 成功访问，窗口内仅有 Form 4/144 | 已验证空集 |
| W-S0 | WDC 窗口内无 8-K/10-K/10-Q（及 /A）申报 | data.sec.gov 成功访问（10-20 复核：最新仍为 10-05 批次；CFO 离职尚无 8-K，期限未满） | 已验证空集（10-20 复核维持） |
| N-S0 | NBIS 窗口内无 6-K（及 /A）申报 | data.sec.gov 成功访问 | 已验证空集 |
| N-P0 | Nebius 窗口内无 **newsroom 新闻稿** | nebius.com/newsroom 成功打开，最新 10-01；**v2 收窄表述：仅限 newsroom 入口；blog 入口窗口内有 4 篇（见 N-P2~N-P5）** | 已验证空集（限 newsroom） |
| W-IR0 | WDC 窗口内 IR 稿件情况 | investor.wdc.com 抓取失败 | 未能验证 |
| M-IR0 | MU 窗口内除 M-P1 外是否另有 IR 稿件 | investors.micron.com 列表页 403 | 未能验证 |

## 三、不计入区（范围外 / 30 日补充 / 待核验 / 评估后未纳入）

v1 不计入区全部沿用（M-S1、M-S2、M-S3、M-P2、W-S1、W-P1、W-N2、N-S1、N-S2、N-P1、E4、V-1~V-5，内容见 v1，此处不重复）。v2 新增：

| 事件 ID | 内容 | 排除原因 |
|---|---|---|
| X-1 | Netlist-Micron $600M 专利和解（[Reuters 10-06](https://www.reuters.com/legal/litigation/micron-enters-600-million-settlement-netlist-patent-dispute-2026-10-06/)；窗口内 Sterne Kessler 通稿 10-15/16） | **评估后未纳入**：事件本体 10-06 窗口外；窗口内仅律所自我宣传通稿与评论，主流财经媒体无窗口内新增事实报道（与 M-N2 的区别：回购窗口内有 Yahoo/Fool/GuruFocus/Barchart 多家主流新增报道） |
| X-2 | MU $50M 美国存储研究承诺（Yahoo 10-17，首轮对照观察项） | **评估后未纳入**：仅单源，未找到第二来源；金额与性质（CSR/研发承诺）对投资叙事影响弱，证据不足 |

## 四、v2 核实记录（本轮新增）

- **Nebius blog 4 篇逐篇打开**：全部为 nebius.com/blog 官方发布的产品/合作公告（非转载），可 curl 全文；发布时间以 Google News RSS 的 GMT 时间戳为准（blog 页面本身不显示时刻，日期级）。
- **WDC CFO**：SMCI 官方 PR 已打开（10-15，"effective immediately"，Mark Long 前任 WDC CFO & Chief Strategy Officer）；Reuters/Fortune/Yahoo 多源；EDGAR WDC 10-20 复核无 8-K（SMCI 侧已发 8-K，Long 接替 David Weigand，生效 10-14）。
- **日期冲突记录**：Rosenblatt 首覆日期有 10-08（RSS，pubDate 呈 07:00:00 GMT 批处理痕迹）与 10-14（$304 目标价，多源一致）两种信号；工会投票日 ~10-09/10（Barchart 10-10）与 Fool 10-19 报道；均按口径 A 以窗口内报道计入并保留原始时间标注。
- **本轮联网请求**：FetchURL 6（nebius blog 列表 + 4 篇 blog + SMCI PR）；WebSearch 5；SEC 1（WDC submissions 复核）。间隔 ≥2s，识别性 UA。
