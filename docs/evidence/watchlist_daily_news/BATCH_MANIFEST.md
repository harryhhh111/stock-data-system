# 证据批次清单（最终矩阵支撑批次 = 批次 B）

## 批次 B（支撑最终矩阵；本目录文件）
抓取时间（北京时间，本机 UTC+8）：Finnhub 2026-10-20 22:26；Google News RSS 2026-10-20 22:28
UA：Finnhub `source-coverage-check research-agent`；RSS `Mozilla/5.0 ... source-coverage-check research-agent`

| 文件 | 查询参数 | 条目数（总数/北京窗口内） |
|---|---|---|
| fh_MU.json | company-news?symbol=MU&from=2026-10-12&to=2026-10-20（UTC 日期） | 248 / 209 |
| fh_WDC.json | 同上 symbol=WDC | 121 / 100 |
| fh_NBIS.json | 同上 symbol=NBIS | 129 / 111 |
| gn_0.xml | q="Micron Technology" when:14d, hl=en-US&gl=US&ceid=US:en | 100 / 83 |
| gn_1.xml | q="Western Digital" when:14d | 100 / 60 |
| gn_2.xml | q="Nebius" when:14d | 100 / 75 |
| gn_3.xml | q="CXMT DRAM" when:14d | 100 / 76 |
| gn_4.xml | q="CoreWeave bond" when:14d | 100 / 90 |
| gn_5.xml | q="Toshiba HDD" when:14d | 53 / 14 |
| fh_bad.json | 无效 token 对照测试（HTTP 401 {"error":"Invalid API key"}） | — |
| tf_list.html / tf_list_p3.html / tf_detail.html | TrendForce 新闻中心第 1 页 / ?page=2 / 4Q26 详情页（2026-10-20 抓取） | — |
| evidence_extract.log | 条目级证据抽取与抽查脚本输出 | — |

注：北京窗口 = [2026-10-13 00:00, 2026-10-20 00:00) = UTC [10-12 16:00, 10-19 16:00)。Finnhub JSON 无 token 字段（token 仅出现在请求 URL，未落盘）；RSS XML 原样存档。

## 批次 A（可访问性报告批次，仅作参考，不支撑矩阵；文件未在本目录）
- 时间：2026-10-20 21:53~21:55（北京）。Finnhub：from=2026-10-13&to=2026-10-19（不覆盖北京窗口起点），结果 JSON 未留存（仅 401 测试件 fh_bad.json 同源留存于本目录）。RSS 8 查询（"Micron OR \"MU\""、"\"Western Digital\""、"Nebius"、DRAM、HBM、memory chip prices、CoreWeave、HDD Toshiba），XML 留存于 /tmp/gn_0b6b96.xml、gn_0cc364.xml、gn_75f0e9.xml、gn_t_*.xml（未复制到本目录）。

## 增补（2026-10-20 收紧轮）：Nebius blog 原文快照
| 文件 | 内容 | 页面内结构化时间元数据（JSON-LD） |
|---|---|---|
| nebius_blog_runai.html | Run:ai 上线稿（N-P2） | datePublished 2026-10-13，dateModified 2026-10-13T16:04:15Z |
| nebius_blog_dli.html | DLI 课程合作稿（N-P3） | datePublished 2026-10-16，dateModified 2026-10-16T13:00:50Z |
| nebius_blog_models.html | 三款开源模型稿（N-P4） | datePublished 2026-10-16，dateModified 2026-10-16T14:42:07Z |
| nebius_blog_builder.html | Builder Program 欧洲扩展稿（N-P5） | datePublished 2026-10-19，dateModified 2026-10-19T09:39:33Z |
用途：N-P2~N-P5 发布时间的独立核验证据（不依赖 RSS pubDate，不依赖 X 帖）。
