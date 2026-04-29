"""个股分析器 — 报告格式化。

支持三种输出格式：text（终端）、markdown、json。
"""

import json
import pandas as pd


def _v(val, default="-"):
    """处理 NaN 值。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    return val


def _fmt_amount(val) -> str:
    """金额格式化为亿。"""
    v = _v(val)
    if v == "-":
        return v
    yi = float(v) / 1e8
    if abs(yi) >= 10000:
        return f"{yi/10000:.2f}万亿"
    return f"{yi:,.1f}亿"


def _fmt_pct(val, decimals=1) -> str:
    """百分比格式化。"""
    v = _v(val)
    if v == "-":
        return v
    return f"{float(v)*100:.{decimals}f}%"


def _fmt_ratio(val) -> str:
    """比率格式化。"""
    v = _v(val)
    if v == "-":
        return v
    return f"{float(v):.2f}"


def _fmt_yoy(val) -> str:
    """同比增长率格式化，正值带 + 号。"""
    v = _v(val)
    if v == "-":
        return v
    pct = float(v) * 100
    if pct > 0:
        return f"+{pct:.0f}%"
    return f"{pct:.0f}%"


def _fmt_pe(val) -> str:
    """PE 格式化，负值显示为亏损。"""
    v = _v(val)
    if v == "-":
        return v
    if float(v) <= 0:
        return "亏损"
    return f"{float(v):.1f}"


def format_report(stock: dict, sections: dict, overall: dict, fmt: str = "text") -> str:
    """主入口：根据 fmt 分发到对应格式化函数。"""
    if fmt == "json":
        return _format_json(stock, sections, overall)
    elif fmt == "md" or fmt == "markdown":
        return _format_markdown(stock, sections, overall)
    else:
        return _format_text(stock, sections, overall)


def _format_text(stock: dict, sections: dict, overall: dict) -> str:
    """终端文本格式报告。"""
    name = _v(stock.get("stock_name"), "未知")
    code = _v(stock.get("stock_code"), "")
    market = _v(stock.get("market"), "")
    industry = _v(stock.get("industry"), "未知行业")
    close = _v(stock.get("close"))
    mcap = _v(stock.get("market_cap"))

    # 标题行
    price_str = f"￥{float(close):.2f}" if close != "-" else "-"
    mcap_str = _fmt_amount(mcap) if mcap != "-" else "-"
    list_date = _v(stock.get("list_date"))
    list_str = str(list_date)[:10] if list_date != "-" else ""

    lines = []
    lines.append("")
    lines.append("═" * 66)
    lines.append(f"  个股分析报告：{code} {name}")
    lines.append(f"  {list_str} | {market} | {industry} | {price_str} | 市值 {mcap_str}")
    lines.append("═" * 66)

    # 一、盈利能力
    prof = sections.get("profitability", {})
    lines.append("")
    lines.append(f"一、盈利能力                         评级：{prof.get('star', '-')}")
    lines.append("─" * 66)
    if prof.get("details"):
        lines.append(f"  {'年份':<6} {'营收':>8} {'同比':>6} {'净利润':>8} {'同比':>6} {'毛利率':>7} {'净利率':>7} {'ROE':>7}")
        for d in prof["details"]:
            lines.append(
                f"  {d['year']:<6} "
                f"{_fmt_amount(d.get('revenue')):>8} "
                f"{_fmt_yoy(d.get('revenue_yoy')):>6} "
                f"{_fmt_amount(d.get('net_profit')):>8} "
                f"{_fmt_yoy(d.get('net_profit_yoy')):>6} "
                f"{_fmt_pct(d.get('gross_margin')):>7} "
                f"{_fmt_pct(d.get('net_margin')):>7} "
                f"{_fmt_pct(d.get('roe')):>7}"
            )
    lines.append(f"  {prof.get('verdict', '-')}")

    # 二、财务健康度
    health = sections.get("health", {})
    lines.append("")
    lines.append(f"二、财务健康度                       评级：{health.get('star', '-')}")
    lines.append("─" * 66)
    hd = health.get("details", {})
    if hd:
        lines.append(f"  资产负债率：{_fmt_pct(hd.get('debt_ratio'))}")
        lines.append(f"  流动比率：{_fmt_ratio(hd.get('current_ratio'))}  |  速动比率：{_fmt_ratio(hd.get('quick_ratio'))}")
        debt_trend = hd.get("debt_trend", [])
        if len(debt_trend) >= 2:
            trend_str = " → ".join(f"{d['year']}: {_fmt_pct(d['debt_ratio'])}" for d in debt_trend)
            lines.append(f"  近{len(debt_trend)}年负债率：{trend_str}")
    lines.append(f"  {health.get('verdict', '-')}")

    # 三、现金流质量
    cf = sections.get("cashflow", {})
    lines.append("")
    lines.append(f"三、现金流质量                       评级：{cf.get('star', '-')}")
    lines.append("─" * 66)
    cd = cf.get("details", {})
    if cd:
        lines.append(f"  数据源：{cd.get('source', '-')}")
        lines.append(f"  经营现金流(CFO)：{_fmt_amount(cd.get('cfo'))}")
        lines.append(f"  资本开支(CAPEX)：{_fmt_amount(cd.get('capex'))}")
        lines.append(f"  自由现金流(FCF)：{_fmt_amount(cd.get('fcf'))}")
        lines.append(f"  CFO/净利润：{_fmt_ratio(cd.get('cfo_quality'))}")
        lines.append(f"  CAPEX/营收：{_fmt_pct(cd.get('capex_intensity'))}")
        fcf_years = cd.get("fcf_years", [])
        if fcf_years:
            lines.append(f"  近{len(fcf_years)}年 FCF：")
            for fy in fcf_years:
                lines.append(f"    {fy['year']}: {_fmt_amount(fy.get('fcf'))}")
    lines.append(f"  {cf.get('verdict', '-')}")

    # 四、估值水平
    val = sections.get("valuation", {})
    lines.append("")
    lines.append(f"四、估值水平                         评级：{val.get('star', '-')}")
    lines.append("─" * 66)
    vd = val.get("details", {})
    if vd:
        pe = vd.get("pe")
        med_pe = vd.get("median_pe")
        pe_line = f"  PE(TTM)：{_fmt_pe(pe)}"
        if med_pe is not None and vd.get("peer_count", 0) > 0:
            pe_vs = vd.get("pe_vs") or "-"
            pe_line += f"    行业中位数：{_fmt_pe(med_pe)}    {pe_vs}"
        lines.append(pe_line)

        pb = vd.get("pb")
        med_pb = vd.get("median_pb")
        pb_line = f"  PB：{_fmt_ratio(pb)}"
        if med_pb is not None and vd.get("peer_count", 0) > 0:
            pb_vs = vd.get("pb_vs") or "-"
            pb_line += f"        行业中位数：{_fmt_ratio(med_pb)}    {pb_vs}"
        lines.append(pb_line)

        fy = vd.get("fcf_yield")
        med_fy = vd.get("median_fcf_yield")
        fy_line = f"  FCF Yield：{_fmt_pct(fy, 2)}"
        if med_fy is not None and vd.get("peer_count", 0) > 0:
            fy_vs = vd.get("fy_vs") or "-"
            fy_line += f"    行业中位数：{_fmt_pct(med_fy, 2)}    {fy_vs}"
        lines.append(fy_line)

        if vd.get("peer_count", 0) > 0:
            lines.append(f"  同行业对比样本：{vd['peer_count']} 只股票")
        else:
            lines.append(f"  （缺少同行业数据，无法比较）")
    lines.append(f"  {val.get('verdict', '-')}")

    # 五、综合评价
    lines.append("")
    lines.append(f"五、综合评价                         评级：{overall.get('star', '-')}")
    lines.append("─" * 66)
    lines.append(f"  {overall.get('verdict', '')}")

    risks = overall.get("risks", [])
    if risks:
        lines.append("")
        lines.append("  风险提示：")
        for r in risks:
            lines.append(f"  ⚠ {r}")

    lines.append("")
    lines.append("═" * 66)
    return "\n".join(lines)


def _format_markdown(stock: dict, sections: dict, overall: dict) -> str:
    """Markdown 格式报告。"""
    name = _v(stock.get("stock_name"), "未知")
    code = _v(stock.get("stock_code"), "")
    market = _v(stock.get("market"), "")
    industry = _v(stock.get("industry"), "未知行业")
    close = _v(stock.get("close"))
    mcap = _v(stock.get("market_cap"))

    price_str = f"￥{float(close):.2f}" if close != "-" else "-"
    mcap_str = _fmt_amount(mcap) if mcap != "-" else "-"

    lines = []
    lines.append(f"# 个股分析报告：{code} {name}")
    lines.append("")
    lines.append(f"**{market}** | {industry} | {price_str} | 市值 {mcap_str}")
    lines.append("")

    # 一、盈利能力
    prof = sections.get("profitability", {})
    lines.append(f"## 一、盈利能力 — {prof.get('star', '-')}")
    lines.append("")
    if prof.get("details"):
        lines.append("| 年份 | 营收 | 同比 | 净利润 | 同比 | 毛利率 | 净利率 | ROE |")
        lines.append("|------|------|------|--------|------|--------|--------|-----|")
        for d in prof["details"]:
            lines.append(
                f"| {d['year']} "
                f"| {_fmt_amount(d.get('revenue'))} "
                f"| {_fmt_yoy(d.get('revenue_yoy'))} "
                f"| {_fmt_amount(d.get('net_profit'))} "
                f"| {_fmt_yoy(d.get('net_profit_yoy'))} "
                f"| {_fmt_pct(d.get('gross_margin'))} "
                f"| {_fmt_pct(d.get('net_margin'))} "
                f"| {_fmt_pct(d.get('roe'))} |"
            )
    lines.append(f"\n{prof.get('verdict', '-')}\n")

    # 二、财务健康度
    health = sections.get("health", {})
    lines.append(f"## 二、财务健康度 — {health.get('star', '-')}")
    lines.append("")
    hd = health.get("details", {})
    if hd:
        lines.append(f"- 资产负债率：{_fmt_pct(hd.get('debt_ratio'))}")
        lines.append(f"- 流动比率：{_fmt_ratio(hd.get('current_ratio'))}")
        lines.append(f"- 速动比率：{_fmt_ratio(hd.get('quick_ratio'))}")
        debt_trend = hd.get("debt_trend", [])
        if debt_trend:
            trend_str = " → ".join(f"{d['year']}: {_fmt_pct(d['debt_ratio'])}" for d in debt_trend)
            lines.append(f"- 负债率趋势：{trend_str}")
    lines.append(f"\n{health.get('verdict', '-')}\n")

    # 三、现金流质量
    cf = sections.get("cashflow", {})
    lines.append(f"## 三、现金流质量 — {cf.get('star', '-')}")
    lines.append("")
    cd = cf.get("details", {})
    if cd:
        lines.append(f"- 数据源：{cd.get('source', '-')}")
        lines.append(f"- CFO：{_fmt_amount(cd.get('cfo'))}")
        lines.append(f"- CAPEX：{_fmt_amount(cd.get('capex'))}")
        lines.append(f"- FCF：{_fmt_amount(cd.get('fcf'))}")
        lines.append(f"- CFO/净利润：{_fmt_ratio(cd.get('cfo_quality'))}")
        lines.append(f"- CAPEX/营收：{_fmt_pct(cd.get('capex_intensity'))}")
    lines.append(f"\n{cf.get('verdict', '-')}\n")

    # 四、估值水平
    val = sections.get("valuation", {})
    lines.append(f"## 四、估值水平 — {val.get('star', '-')}")
    lines.append("")
    vd = val.get("details", {})
    if vd:
        lines.append(f"| 指标 | 当前值 | 行业中位数 | 比较 |")
        lines.append(f"|------|--------|-----------|------|")
        pe_vs = vd.get("pe_vs") or "-"
        pe_line = f"| PE(TTM) | {_fmt_pe(vd.get('pe'))} | {_fmt_pe(vd.get('median_pe'))} | {pe_vs} |"
        lines.append(pe_line)
        pb_vs = vd.get("pb_vs") or "-"
        pb_line = f"| PB | {_fmt_ratio(vd.get('pb'))} | {_fmt_ratio(vd.get('median_pb'))} | {pb_vs} |"
        lines.append(pb_line)
        fy_vs = vd.get("fy_vs") or "-"
        fy_line = f"| FCF Yield | {_fmt_pct(vd.get('fcf_yield'), 2)} | {_fmt_pct(vd.get('median_fcf_yield'), 2)} | {fy_vs} |"
        lines.append(fy_line)
        if vd.get("peer_count", 0) > 0:
            lines.append(f"\n同行业对比：{vd['peer_count']} 只股票")
    lines.append(f"\n{val.get('verdict', '-')}\n")

    # 五、综合评价
    lines.append(f"## 五、综合评价 — {overall.get('star', '-')}")
    lines.append("")
    lines.append(overall.get("verdict", ""))
    risks = overall.get("risks", [])
    if risks:
        lines.append("")
        lines.append("### 风险提示")
        for r in risks:
            lines.append(f"- ⚠ {r}")
    lines.append("")

    return "\n".join(lines)


def _format_json(stock: dict, sections: dict, overall: dict) -> str:
    """JSON 格式输出。"""
    # Convert non-serializable values
    def clean(obj):
        if isinstance(obj, dict):
            return {k: clean(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [clean(v) for v in obj]
        elif isinstance(obj, float) and pd.isna(obj):
            return None
        elif obj is pd.NaT:
            return None
        return obj

    result = {
        "stock": clean(stock),
        "sections": clean(sections),
        "overall": clean(overall),
    }
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)
