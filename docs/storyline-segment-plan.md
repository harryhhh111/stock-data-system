# 「业务构成」方案 v2:A股结构化回填(港股/美股 LLM 路线已砍)

## v2 变更说明(DeepSeek 评审后)

**砍掉港股/美股 LLM 路线**,理由(评审实测 + 本人复核认可):

1. 模型知识截止导致"最新期"是假的——实测 kimi 提取腾讯返回 2024 年报,落后两年;校验只能验形式(ratio 合计 1.0 也合法),验不了内容,违反项目 NO SILENT FAILURE 原则
2. 单位量纲 LLM 不可控(实测返回"亿元" vs 东财"元",差 1 亿倍)
3. mmx CLI 本机不存在(那是海外服务器的);港股/美股段数据归属还有两库不互通的矛盾
4. 港股正确方向是结构化数据源(港交所年报 PDF 分部报告是法定披露项),后续单独立项

**修正事实错误**:`stock_info.em_code` 对 CN_A **100% 为 NULL**(复核确认 0/5493),东财代码必须由 stock_code 推导。

## 已验证的事实(全部复核过)

- A股接口:`https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax?code=SH600519` → `zygcfx`,字段 `REPORT_DATE / MAINOP_TYPE / ITEM_NAME / MAIN_BUSINESS_INCOME(单位:元) / MBI_RATIO(0~1) / GROSS_RPOFIT_RATIO(0~1)`,600519 有 200 条 / 37 期
- MAINOP_TYPE:1=行业、2=产品、3=地区
- 前缀分布(实查):SH=600/601/603/605/688/689(2248只)、SZ=000/001/002/003/300/301/302(2945只)、BJ=920(300只,北交所,东财有数据)
- 限速参数代码路径是 `config.throttle.base_delay`(不是直接用 env 名)
- 前端无现成 segmented 组件,复制 storyline-page.tsx 范围选择器的内联样式
- 数据量:约 200 条/股 × 5493 ≈ 110 万行,PG 无压力,(stock_code, report_date) 索引必备

## 实施步骤

### 1. 建表 `scripts/add_stock_segment.sql`(并在数据库执行)

```sql
CREATE TABLE IF NOT EXISTS stock_segment (
    id            BIGSERIAL PRIMARY KEY,
    stock_code    VARCHAR(20) NOT NULL REFERENCES stock_info(stock_code),
    report_date   DATE NOT NULL,               -- 财报期末日
    dimension     VARCHAR(10) NOT NULL,        -- product | industry | region
    item_name     VARCHAR(200) NOT NULL,
    revenue       NUMERIC(20,2),               -- 单位:元
    revenue_ratio NUMERIC(8,4),                -- 0~1
    gross_margin  NUMERIC(8,4),                -- 0~1
    source        VARCHAR(40) NOT NULL,        -- v1 只有 'em_f10'
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_stock_segment UNIQUE (stock_code, report_date, dimension, item_name, source)
);
CREATE INDEX idx_stock_segment_code ON stock_segment(stock_code, report_date DESC);
```

### 2. A股回填(fetcher + 脚本)

- `core/fetchers/segment.py::fetch_cn_a_segment(stock_code)`:
  - 东财代码推导:`6xx → SH{code}`、`0xx/3xx → SZ{code}`、`920 → BJ{code}`(兜底:其他 8xx/4xx → BJ)
  - 调 BusinessAnalysis 接口,解析 `zygcfx`,MAINOP_TYPE 映射 1→industry / 2→product / 3→region
  - 原始响应存 `raw_snapshot`(Layer 0 惯例)
- `scripts/backfill_segment_cn_a.py`:
  - 全部 CN_A 5493 只;限速用 `config.throttle.base_delay/max_delay`;失败重试 + 断点续跑(已有数据的股票跳过);批量 upsert,source='em_f10'
  - 支持 `--codes 600519,000858` 小批试跑、`--limit N`
  - 全量后台跑,预计 1.5–3 小时;日志落 `logs/`

### 3. 后端:timeline 接口加 segments

- `web/services/storyline_service.py::_get_segments(stock_code)`:查 `stock_segment`,按 report_date 倒序取最近 6 期,每期按 dimension 分组
- `get_timeline` 返回增加 `segments: [{report_date, source, dimensions: {product: [...], industry: [...], region: [...]}}]`,条目字段 `item_name / revenue / revenue_ratio / gross_margin`
- 港股/美股查不到 → `segments: []`,前端显示"暂无业务构成数据"(诚实空态,不编造)

### 4. 前端:「业务构成」区块

- `frontend/src/components/storyline/segment-panel.tsx`:
  - 位置:股票信息行下方、K 线上方
  - 最新一期;维度切换(产品/行业/地区)——复制 storyline-page.tsx:107-122 范围选择器的内联样式(项目无 segmented 组件);无数据的维度不渲染对应按钮
  - 列表:项目名 + 占比横条 + 占比% + 收入(fmtYi)+ 毛利率,按占比降序
  - 期别标注:"数据期:2026 中报" 字样,避免误解为最新季度
- 类型:`frontend/src/lib/types/storyline.ts` 加 `StorylineSegment` / `StorylinePeriodSegments`;页面接线渲染

### 5. 验证

- 建表 → 试跑 `--codes 600519,000858,BJ 920002 对应的 920002` → curl 检查结构与数值(抽 600519 茅台酒占比 85.7% 与东财页面比对)
- `npm run build`;页面目视茅台
- 全量回填后台跑完抽查 10 只

### 6. 明确不做(v1 边界)

- 港股/美股:不做(LLM 路线已砍;港交所 PDF 结构化提取后续单独立项)
- 不做历年构成变迁图(stacked bar),后续迭代
- 不接入 scheduler 自动更新,财报季手动重跑脚本

## 后续立项备忘(不在本方案)

- **港股业务构成**:港交所年报/中报 PDF 的分部报告(法定披露),或继续逆向东财港股 F10 懒加载 chunk(需浏览器抓包)
- **美股业务构成**:SEC XBRL segment 维度的 facts(我们已有 SEC 抓取管线,`core/fetchers/us_financial.py`),写进海外库而非国内库
