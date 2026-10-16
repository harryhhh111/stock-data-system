# 关注股看板运行说明

本次只包含关注池与日线展示，不包含新闻采集、实时行情或自动交易。

## 启用

1. 在 US 元数据实例执行 `scripts/watchlist_tables.sql`。SQL 可重复执行，不修改已有财务、行情表。
2. 为后端配置 `STOCK_WATCHLIST_ADMIN_TOKEN`，使用至少 24 字符的随机秘密，并重启后端。不要将凭据提交仓库、写入前端构建变量或放到 URL。
3. 构建发布现有前端，打开 `/watchlist`（现在是首页）。开发沿用 Vite 代理；生产分别配置 `VITE_US_API_URL` 和 `VITE_CN_API_URL`。
4. 在页面“编辑凭据”或添加弹窗中输入凭据。它只保存在当前页面内存，刷新后需重新输入。未配置时写入返回 503，错误凭据返回 401。

当前是单个共享关注池，读取沿用项目现有公开 API 边界；备注不应包含机密材料。生产须使用 HTTPS 和已有网络访问控制。这不是团队成员权限系统。

## 接口

统一前缀 `/api/v1`，沿用 `{ok, data}` envelope。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/watchlist` | 元数据列表，US 实例 |
| POST | `/watchlist` | 添加 market、stock_code、stock_name、group_name、note、research_url |
| PATCH | `/watchlist/{id}` | 替换分组、理由、研究链接；不改变证券身份 |
| DELETE | `/watchlist/{id}` | 移除关注项，不删除行情和研究 |
| POST | `/watchlist/quotes` | 按市场读取日线；body 为 market、codes（最多 200 个），前端自动拆批 |
| POST | `/watchlist/suggest` | AI 起草分组与关注理由；需登录态；依赖本机 mmx CLI 与研究文档仓库 |
| POST | `/watchlist/login` | body `{token}`；验证管理凭据并签发 HttpOnly 会话 Cookie（30 天） |
| POST | `/watchlist/logout` | 清除会话 Cookie |
| GET | `/watchlist/session` | 查询当前会话是否已登录 |

## 登录方式

页面右上角「管理员登录」输入一次凭据即可，30 天内免输；会话为 HttpOnly Cookie（JS 不可读），写操作同时兼容 `Authorization: Bearer`（脚本/API 调用用）。注意：当前前端为 HTTP 明文访问，Cookie 与凭据在传输中均可被同网络监听者看到——生产化需上 HTTPS 后再放宽访问面。

## AI 生成建议依赖

- 后端以 subprocess 调用本机 `mmx` CLI（MiniMax）：需 `npm i -g mmx-cli` 并完成 `mmx auth login`；密钥由 mmx 管理，本仓库不接触。可用 `STOCK_WATCHLIST_MMX_BIN` 指定路径。
- 关注理由按 `research.yaml` 版本 2 的市场和证券代码匹配，读取指定的观点、风险、指标和待验证问题章节；不再猜测目录名或截取正文开头。默认研究路径为仓库旁 `../stock-research`，可用 `STOCK_RESEARCH_REPO_PATH` 覆盖；该路径必须是 Git 仓库根目录，后端运行环境需要 Git 与 PyYAML。
- 每次生成固定一个 Git HEAD SHA，只读取该提交中受跟踪的 YAML/Markdown；本地未提交修改不生效，请先审核并提交研究。后端不自动 pull、不执行研究脚本。模型输入仍会发送给 MiniMax，请勿将不适合发送的敏感内容纳入研究生成。
- `/watchlist/suggest` 保留原字段，追加 `research`：status（matched/no_entry）、entity_id、commit_sha、sections（kind/path/line/heading_path/truncated/total_chars）、missing_sections。每类摘录正文最多 1500 字符，共最多 6000；版本号不等于研究数据截止日。页面提示缺失及截断，不宣称已完成事实核验。
- 仓库和入口有效但证券未配置入口时，生成标明 `[通用草稿]` 的内容。仓库缺失、版本不支持、入口损坏、路径越界、已配置标题不存在或证券有歧义时返回带“研究读取失败”详情的 503，不调用模型，也不伪装成没有研究。null 章节允许缺省，但命中主体后必须至少有一个可用观点章节。
- 生成内容为未确认的草稿：前端填入可编辑字段，由用户修改后随保存入库；接口失败不影响手动填写。

元数据写入要求 `Authorization: Bearer <token>`。重复返回 409，项不存在返回 404，参数非法返回 422，数据库不可用返回 503。
报价只允许服务器 `STOCK_MARKETS` 配置的市场；对应市场未发布新接口时，页面明确显示行情请求错误。

## 数据口径

- 每 5 分钟读取数据库，不触发同步，不是实时行情。日期是该股票最新入库交易日。
- 涨跌幅为原始收盘相比前一条日线记录，不是总回报或复权收益。趋势最多 20 条记录；缺失交易日不凭空补齐。
- 表头明确为“最新收盘价”“涨跌幅（较前次收盘）”“近 20 个交易日走势”“最新行情日期”。涨跌幅每行展示实际比较的两个日期，接口新增 `previous_quote_date`；缺少前次记录时为 null。遇到日线缺口不宣称严格单日涨跌，走势也不是该百分比的累计区间。
- 折线旁显示实际记录数及起止日期；不足 20 条按实际长度展示。缺失收盘保持断点，价格口径待核对时暂停绘图并明确说明。前端兼容旧版 API，但未提供比较日期时明确提示，不推算前一个自然日。
- 原始日线未验证公司行动。窗口内超过 35% 的跳变会隐藏涨跌幅和走势并提示核对；低于阈值不保证已排除拆股或除权影响。
- 滞后标记使用超过 4 个自然日的简化规则，不是交易日历判断，长假可能触发提示。
- 研究链接仅支持 HTTP(S)，不支持本机绝对路径。
- 搜索不到的公司可手动填写代码和名称加入关注池；系统不校验证券身份，不自动同步该代码的数据。
- 当前机器仅验收 US，国内实例需单独发布相同代码，不在本机跨库读取。

## 验证命令

`venv/bin/python -m pip install -r requirements-dev.txt`

`venv/bin/python -m pytest tests/test_web/test_research_reader.py tests/test_web/test_watchlist.py -q`

`cd frontend && npm run build`

测试覆盖权限、错误状态、参数边界、事务提交及行情计算。真实数据库与浏览器验收结果见 MVP 方案记录。
