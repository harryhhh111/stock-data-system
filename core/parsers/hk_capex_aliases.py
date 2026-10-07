"""港股 capex 行名别名表 + 行文本归一化 + 单位识别（T2，移植自 /tmp/hkex_samples/aliases.py）。

改进点（相对调研原型）：
- 单位识别拆为「币种」与「量级」两条独立正则链再组合，修复 00388 型陷阱
  （页头同时出现「（財務數字以港元為單位）」与列头「百萬元」，原型只命中
  前者返回 HKD_unspec，组合后正确得到 HKD_mn）。
- 排除词不含「投資」——友邦「投資物業以及物業、廠房及設備付款」必须命中。
"""
from __future__ import annotations

import re

_T2S = {
    "資": "资", "產": "产", "開": "开", "廠": "厂", "設": "设", "備": "备",
    "購": "购", "買": "买", "業": "业", "樓": "楼", "價": "价", "無": "无",
    "項": "项", "權": "权", "證": "证", "於": "于", "為": "为",
    "現": "现", "額": "额", "幣": "币", "億": "亿", "萬": "万", "圓": "圆",
    "與": "与", "併": "并", "屬": "属", "團": "团", "內": "内", "對": "对",
    "處": "处", "變": "变", "這": "这", "體": "体",
    "係": "系", "關": "关", "聯": "联", "營": "营", "舊": "旧", "賃": "赁",
    "裝": "装", "訂": "订", "閱": "阅", "遞": "递", "延": "延", "稅": "税",
    "礦": "矿", "動": "动", "產": "产",
}

# capex 行名别名（繁简英，归一化后做子串匹配）
ALIASES = [
    # 中文
    "已付資本性開支",
    "資本開支",
    "資本支出",
    "資本開支總額",
    "資本性支出",
    "購置物業、廠房及設備",
    "購買物業、廠房及設備",
    "收購物業、廠房及設備",
    "購置物業廠房及設備",
    "購置物業、設備及無形資產",
    "購置物業、設備與無形資產",
    "購置物業及設備",
    "購買物業及設備",
    "購置物業、設備",
    "購置物業、機器及設備",
    "購入物業、機器及設備",
    "購置固定資產",
    "購買固定資產",
    "購入固定資產",
    "購建固定資產、無形資產和其他長期資產支付的現金",
    "購建固定資產、無形資產和其他長期資產",
    "購建固定資產",
    "增置固定資產",
    "購置無形資產",
    "購買無形資產",
    "購買土地使用權",
    "購置土地使用權",
    # 不動產(台/部分HK公司用法)
    "購置不動產、廠房及設備",
    "購買不動產、廠房及設備",
    # 付款型(保险/港交所等)
    "投資物業以及物業、廠房及設備付款",
    "物業、廠房及設備付款",
    "物業、廠房及設備之付款",
    "購置固定資產及無形資產所支付款項",
    "購置物業、廠房及設備所支付款項",
    "無形資產付款",
    # 英文
    "purchase of property, plant and equipment",
    "purchases of property, plant and equipment",
    "payments for property, plant and equipment",
    "payment for property, plant and equipment",
    "payments for purchases of property, plant and equipment",
    "purchases or prepayments of property, plant and equipment and intangible assets",
    "purchase or prepayment of property, plant and equipment",
    "payments to acquire property, plant and equipment",
    "purchases of property plant and equipment",
    "payment to acquire property, plant and equipment",
    "capital expenditure paid",
    "capital expenditure",
    "capital expenditures",
    "payments for capital expenditure",
    "purchase of fixed assets",
    "purchases of fixed assets",
    "payments for fixed assets",
    "additions to property, plant and equipment",
    "payments for property and equipment",
    "purchase of property and equipment",
    "acquisition of property, plant and equipment",
    "acquisition of fixed assets",
    "purchase of intangible assets",
    "purchases of intangible assets",
]

# 合并行白名单（含投资物业等排除词，但整体是 capex 性质）
COMBINED_OK = [
    "購置物業、廠房及設備以及投資物業",
    "購置物業、廠房及設備及投資物業",
    "purchase of property, plant and equipment and investment properties",
    "purchases of property, plant and equipment and investment properties",
    "payments for property, plant and equipment and investment properties",
]

# 排除词：处置/出售/所得/存款/折旧减值类(注意: 不放"投資"——"投資物業"是资产类别)
EXCLUDE_PAT = re.compile(
    r"出售|變現|处置|变现|所得|收回|存入|抵押|按公平值|公允价值|折旧|折舊|撇減|減值|减值"
    r"|redemption|withdraw|matur|disposal|proceeds|deposit|fair ?value|placed|time deposits"
    r"|depreciation|amortis|impair",
    re.I,
)
EXCLUDE_HEAD = re.compile(r"^(出售|變現|处置|变现)\S{0,4}(物業|物业|固定資產|固定资产)|^(disposal|proceeds)", re.I)
PURCHASE_HINT = re.compile(r"購|购|purchase|payment|capital|acquire|addition|收購|收购", re.I)

# R1 行级竞争：总部物业行单列不计（如港交所购置总部物业）；纯无形资产/土地使用权行降权
HQ_PROPERTY_PAT = re.compile(r"總部物業|总部物业")
PPE_PAT = re.compile(
    r"物業|物业|不動產|不动产|廠房|厂房|property|plant|設備|设备|"
    r"固定資產|固定资产|fixed ?asset",
    re.I,
)


def t2s(s: str) -> str:
    for k, v in _T2S.items():
        s = s.replace(k, v)
    return s


def normalize_label(s: str) -> str:
    """小写、繁->简、去空白/标点/连接词(及/与/和/and/、/,/./-/–)。"""
    s = s.lower().strip()
    for a, b in [("（", "("), ("）", ")"), ("　", ""), (" ", ""), ("、", ""), ("，", ""), (",", ""),
                 ("·", ""), (".", ""), ("-", ""), ("–", ""), ("—", ""), ("及", ""), ("与", ""),
                 ("和", ""), ("and", "")]:
        s = s.replace(a, b)
    s = t2s(s)
    return s.rstrip("()（）0123456789:")


_NORM_ALIASES = sorted({normalize_label(a) for a in ALIASES}, key=len, reverse=True)
_NORM_COMBINED = {normalize_label(a) for a in COMBINED_OK}


def match_alias(label_raw: str):
    """匹配 capex 行名。返回 (命中的别名, 是否弱匹配) 或 None。"""
    lab = normalize_label(label_raw)
    if not lab:
        return None
    if EXCLUDE_HEAD.search(lab):
        return None
    excluded = bool(EXCLUDE_PAT.search(lab))
    for c in _NORM_COMBINED:
        if c in lab:
            return (c, False)
    if excluded:
        return None
    for a in _NORM_ALIASES:
        if a and a in lab:
            return (a, False)
    # 弱匹配：行名以购买动词开头且包含资产名词之一（捕捉别名表未列写法）
    if PURCHASE_HINT.search(lab) and re.search(
        r"物业|厂房|设备|固定资产|无形资产|土地|property|plant|equipment|fixedasset|intangible", lab
    ):
        return ("(weak)", True)
    return None


NUM_TOKEN = re.compile(r"^\(?-?[\d][\d,]*(?:\.\d+)?\)?$|^[–—−\-]$")


def parse_num(tok: str):
    """解析数字 token（支持千分位/括号负数/破折号零）。失败返回 None。"""
    t = tok.strip().replace(",", "").replace("，", "")
    if t in ("–", "—", "−", "-", "－"):
        return 0.0
    neg = t.startswith("(") and t.endswith(")")
    if neg:
        t = t[1:-1]
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", t):
        return None
    v = float(t)
    return -v if neg else v


# ── 单位识别（币种与量级分离，再组合）────────────────────────
_CURRENCY_PAT = [
    (r"人民幣|人民币|rmb|renminbi", "RMB"),
    (r"港幣|港币|港元|hk\$|hong ?kong ?dollar", "HKD"),
    (r"美元|us\$|usd|united ?states ?dollar", "USD"),
]
# 量级：具体到宽泛；None 表示未标明量级
_MAGNITUDE_PAT = [
    (r"億元|亿元", 1e8, "100m"),
    (r"百萬元|百万元|百萬|百万", 1e6, "mn"),
    (r"萬元|万元", 1e4, "10k"),
    (r"千元", 1e3, "thousand"),
]
# 英文常见写法（in millions of / RMB'000 / $m 等）。'000 必须带币种前缀，
# 否则数据里的 "1,000" 会误命中（汇丰案例）。
_EN_MAGNITUDE_PAT = [
    (r"(?:in )?millions?\s+of|\bmillions?\b|(?:us\$|hk\$|rmb)\s*m\b|\$\s*m\b", 1e6, "mn"),
    (r"(?:in )?thousands?\b|(?:us\$|hk\$|rmb)\s*'?(?:[’'])000\b", 1e3, "thousand"),
]


def detect_currency(text: str):
    """页/行文本 → 币种 ('RMB'|'HKD'|'USD') 或 None。"""
    low = text.lower()
    for pat, cur in _CURRENCY_PAT:
        if re.search(pat, low):
            return cur
    return None


def detect_magnitude(text: str):
    """文本 → (倍率, 量级标签) 或 (None, None)（未标明量级）。"""
    low = text.lower()
    for pat, mult, name in _MAGNITUDE_PAT:
        if re.search(pat, low):
            return mult, name
    for pat, mult, name in _EN_MAGNITUDE_PAT:
        if re.search(pat, low):
            return mult, name
    return None, None


def unit_scale(text: str):
    """页文本 → (倍率, 单位标签)。币种+量级组合；量级缺失时倍率为 None。

    例：'（財務數字以港元為單位）…百萬元' → (1e6, 'HKD_mn')
        '人民幣千元' → (1e3, 'RMB_thousand')
        '百萬美元' → (1e6, 'USD_mn')
    """
    cur = detect_currency(text)
    mult, mag = detect_magnitude(text)
    if mult is None:
        return None, f"{cur}_unspec" if cur else None
    label = f"{cur}_{mag}" if cur else mag
    return mult, label
