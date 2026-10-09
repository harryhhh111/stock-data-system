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

元数据写入要求 `Authorization: Bearer <token>`。重复返回 409，项不存在返回 404，参数非法返回 422，数据库不可用返回 503。
报价只允许服务器 `STOCK_MARKETS` 配置的市场；对应市场未发布新接口时，页面明确显示行情请求错误。

## 数据口径

- 每 5 分钟读取数据库，不触发同步，不是实时行情。日期是该股票最新入库交易日。
- 涨跌幅为原始收盘相比前一条日线记录，不是总回报或复权收益。趋势最多 20 条记录；缺失交易日不凭空补齐。
- 原始日线未验证公司行动。窗口内超过 35% 的跳变会隐藏涨跌幅和走势并提示核对；低于阈值不保证已排除拆股或除权影响。
- 滞后标记使用超过 4 个自然日的简化规则，不是交易日历判断，长假可能触发提示。
- 研究链接仅支持 HTTP(S)，不支持本机绝对路径。
- 搜索不到的公司可手动填写代码和名称加入关注池；系统不校验证券身份，不自动同步该代码的数据。
- 当前机器仅验收 US，国内实例需单独发布相同代码，不在本机跨库读取。

## 验证命令

`venv/bin/python -m pip install -r requirements-dev.txt`

`venv/bin/python -m pytest tests/test_web/test_watchlist.py -q`

`cd frontend && npm run build`

测试覆盖权限、错误状态、参数边界、事务提交及行情计算。真实数据库与浏览器验收结果见 MVP 方案记录。
