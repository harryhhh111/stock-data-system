"""
transformers/field_mappings.py — 东方财富/港股字段映射常量
将 API 返回的列名/字段名映射为标准数据库字段名。
"""

# ============================================================
# A 股东方财富 — 利润表字段映射
# ============================================================
EM_INCOME_FIELDS: dict[str, str] = {
    # 元数据
    "SECURITY_CODE": "stock_code",
    "REPORT_DATE": "report_date",
    "REPORT_TYPE": "report_type",
    "NOTICE_DATE": "notice_date",
    "UPDATE_DATE": "update_date",
    "CURRENCY": "currency",
    # 收入
    "TOTAL_OPERATE_INCOME": "total_revenue",
    "OPERATE_INCOME": "operating_revenue",
    "OPERATE_COST": "operating_cost",
    # 费用
    "SALE_EXPENSE": "selling_expense",
    "MANAGE_EXPENSE": "admin_expense",
    "FINANCE_EXPENSE": "finance_expense",
    "ME_RESEARCH_EXPENSE": "rd_expense",  # 管理费用中的研发
    "RESEARCH_EXPENSE": "rd_expense",     # 单独的研发费用
    # 利润
    "OPERATE_PROFIT": "operating_profit",
    "TOTAL_PROFIT": "total_profit",
    "INCOME_TAX": "income_tax",
    "NETPROFIT": "net_profit",
    "PARENT_NETPROFIT": "parent_net_profit",
    "DEDUCT_PARENT_NETPROFIT": "net_profit_excl",
    "MINORITY_INTEREST": "minority_interest",
    # 综合收益
    "OTHER_COMPRE_INCOME": "other_comprehensive",
    "TOTAL_COMPRE_INCOME": "total_comprehensive",
    # 每股
    "BASIC_EPS": "eps_basic",
    "DILUTED_EPS": "eps_diluted",
}

# ============================================================
# A 股东方财富 — 资产负债表字段映射
# ============================================================
EM_BALANCE_FIELDS: dict[str, str] = {
    # 元数据
    "SECURITY_CODE": "stock_code",
    "REPORT_DATE": "report_date",
    "REPORT_TYPE": "report_type",
    "NOTICE_DATE": "notice_date",
    "UPDATE_DATE": "update_date",
    "CURRENCY": "currency",
    # 资产
    "MONETARYFUNDS": "cash_equivalents",
    "TRADING_ASSETS": "trading_assets",
    "ACCOUNTS_RECE": "accounts_receivable",
    "NOTES_RECE": "notes_receivable",
    "PREPAYMENT": "prepayments",
    "OTHER_RECE": "other_receivables",
    "INVENTORY": "inventory",
    "CONTRACT_ASSET": "contract_assets",
    "TOTAL_CURRENT_ASSETS": "current_assets",
    "LONG_EQUITY_INVEST": "long_equity_invest",
    "FIXED_ASSET": "fixed_assets",
    "CIP": "construction_in_prog",  # 在建工程
    "INTANGIBLE_ASSET": "intangible_assets",
    "GOODWILL": "goodwill",
    "DEFER_TAX_ASSET": "long_deferred_tax",
    "TOTAL_NONCURRENT_ASSETS": "non_current_assets",
    "TOTAL_ASSETS": "total_assets",
    # 负债
    "SHORT_LOAN": "short_term_borrow",
    "ACCOUNTS_PAYABLE": "accounts_payable",
    "NOTE_ACCOUNTS_PAYABLE": "note_payable",
    "CONTRACT_LIAB": "contract_liab",
    "STAFF_SALARY_PAYABLE": "employee_payable",
    "TAX_PAYABLE": "tax_payable",
    "LONG_LOAN": "long_term_borrow",
    "BOND_PAYABLE": "bonds_payable",
    "DEFER_TAX_LIAB": "long_deferred_liab",
    "TOTAL_NONCURRENT_LIAB": "non_current_liab",
    "TOTAL_CURRENT_LIAB": "current_liab",
    "TOTAL_LIABILITIES": "total_liab",
    # 权益
    "SHARE_CAPITAL": "paid_in_capital",
    "CAPITAL_RESERVE": "capital_reserve",
    "SURPLUS_RESERVE": "surplus_reserve",
    "UNASSIGN_RPOFIT": "retained_earnings",
    "MINORITY_EQUITY": "minority_equity",
    "TOTAL_EQUITY": "total_equity",
    "TOTAL_PARENT_EQUITY": "parent_equity",
}

# ============================================================
# A 股东方财富 — 现金流量表字段映射
# ============================================================
EM_CASHFLOW_FIELDS: dict[str, str] = {
    # 元数据
    "SECURITY_CODE": "stock_code",
    "REPORT_DATE": "report_date",
    "REPORT_TYPE": "report_type",
    "NOTICE_DATE": "notice_date",
    "UPDATE_DATE": "update_date",
    "CURRENCY": "currency",
    # 经营活动
    "NETCASH_OPERATE": "cfo_net",
    "SALES_SERVICES": "cfo_sales",
    "RECEIVE_TAX_REFUND": "cfo_tax_refund",
    "RECEIVE_OTHER_OPERATE": "cfo_operating_receive",
    # 投资活动
    "NETCASH_INVEST": "cfi_net",
    "WITHDRAW_INVEST": "cfi_disposal",
    "CONSTRUCT_LONG_ASSET": "capex",
    "INVEST_PAY_CASH": "cfi_invest_paid",
    # 筹资活动
    "NETCASH_FINANCE": "cff_net",
    "BORROW_FUND_ADD": "cff_borrow_received",
    "PAY_DEBT_CASH": "cff_borrow_repaid",
    "ASSIGN_DIVIDEND_PORFIT": "cff_dividend_paid",
    # 汇率及现金
    "RATE_CHANGE_EFFECT": "fx_effect",
    "CCE_ADD": "cash_increase",
    "BEGIN_CCE": "cash_begin",
    "END_CCE": "cash_end",
}

# ============================================================
# REPORT_TYPE 映射（中文 → 标准）
# ============================================================
REPORT_TYPE_MAP: dict[str, str] = {
    "年报": "annual",
    "中报": "semi",
    "一季报": "quarterly",
    "三季报": "quarterly",
}

# ============================================================
# 港股 DATE_TYPE_CODE → 标准 report_type
# ============================================================
HK_DATE_TYPE_MAP: dict[str, str] = {
    "001": "annual",     # 年报
    "002": "semi",       # 中报
    "003": "quarterly",  # 一季报
    "004": "quarterly",  # 三季报
}


# ============================================================
# 港股 — 利润表字段映射（中文字段名 → 标准字段名）
# ============================================================
HK_INCOME_FIELDS: dict[str, str] = {
    "营业额": "total_revenue",
    "其他营业收入": "other_revenue",
    "营运收入": "operating_revenue",
    "营运支出": "operating_cost",
    "毛利": "gross_profit",
    "销售及分销费用": "selling_expense",
    "行政开支": "admin_expense",
    "经营溢利": "operating_profit",
    "其他收益": "other_income",
    "其他收入": "other_income",
    "其他支出": "other_expense",
    "利息收入": "interest_income",
    "融资成本": "finance_expense",
    "应占联营公司溢利": "assoc_profit",
    "应占合营公司溢利": "joint_profit",
    "溢利其他项目": "profit_other",
    "除税前溢利": "total_profit",
    "税项": "income_tax",
    "持续经营业务税后利润": "continued_profit",
    "除税后溢利": "net_profit",
    "少数股东损益": "minority_interest",
    "股东应占溢利": "parent_net_profit",
    "每股基本盈利": "eps_basic",
    "每股摊薄盈利": "eps_diluted",
    "每股股息": "dps",
    "其他全面收益": "other_comprehensive",
    "全面收益总额": "total_comprehensive",
}

# ============================================================
# 港股 — 资产负债表字段映射
# ============================================================
HK_BALANCE_FIELDS: dict[str, str] = {
    "现金及等价物": "cash_equivalents",
    "短期存款": "short_term_deposit",
    "应收帐款": "accounts_receivable",
    "预付款项": "prepayments",
    "存货": "inventory",
    "其他非流动资产": "other_non_current_assets",
    "物业厂房及设备": "property_plant_equipment",
    "固定资产": "fixed_assets",
    "在建工程": "construction_in_prog",
    "投资物业": "investment_property",
    "无形资产": "intangible_assets",
    "土地使用权": "land_use_rights",
    "使用权资产": "right_of_use_assets",
    "联营公司权益": "assoc_equity",
    "合营公司权益": "joint_equity",
    "持有至到期投资": "held_to_maturity_invest",
    "可供出售投资": "available_for_sale_invest",
    "交易性金融资产(流动)": "trading_assets",
    "递延税项资产": "deferred_tax_asset",
    "流动资产合计": "current_assets",
    "非流动资产合计": "non_current_assets",
    "总资产": "total_assets",
    "应付帐款": "accounts_payable",
    "应付票据": "notes_payable",
    "应付税项": "tax_payable",
    "应付股利": "dividend_payable",
    "短期贷款": "short_term_borrow",
    "长期贷款": "long_term_borrow",
    "长期应付款": "long_term_payable",
    "递延税项负债": "deferred_tax_liab",
    "流动负债合计": "current_liab",
    "非流动负债合计": "non_current_liab",
    "总负债": "total_liab",
    "股本": "paid_in_capital",
    "股本溢价": "capital_reserve",
    "储备": "reserves",
    "其他储备": "other_reserves",
    "保留溢利(累计亏损)": "retained_earnings",
    "少数股东权益": "minority_equity",
    "库存股": "treasury_shares",
    "股东权益": "shareholder_equity",
    "总权益": "total_equity",
}

# ============================================================
# 港股 — 现金流量表字段映射
# ============================================================
HK_CASHFLOW_FIELDS: dict[str, str] = {
    "经营业务现金净额": "cfo_net",
    "营运资金变动前经营溢利": "cfo_operating_profit",
    "加:折旧及摊销": "cfo_depreciation",
    "加:利息支出": "cfo_interest_expense",
    "加:减值及拨备": "cfo_impairment",
    "已付税项": "cfo_tax_paid",
    "已付利息(经营)": "cfo_interest_paid",
    "应收帐款减少": "cfo_ar_decrease",
    "应付帐款及应计费用增加(减少)": "cfo_ap_increase",
    "存货(增加)减少": "cfo_inventory_decrease",
    "投资业务现金净额": "cfi_net",
    "已收利息(投资)": "cfi_interest_received",
    "已收股息(投资)": "cfi_dividend_received",
    "收回投资所得现金": "cfi_disposal",
    "投资支付现金": "cfi_invest_paid",
    "购建固定资产": "capex",
    "购建无形资产及其他资产": "capex_intangible",
    "处置固定资产": "cfi_disposal_fa",
    "融资业务现金净额": "cff_net",
    "新增借款": "cff_borrow_received",
    "偿还借款": "cff_borrow_repaid",
    "已付股息(融资)": "cff_dividend_paid",
    "已付利息(融资)": "cff_interest_paid",
    "发行股份": "cff_share_issued",
    "回购股份": "cff_share_buyback",
    "期初现金": "cash_begin",
    "期末现金": "cash_end",
    "现金净额": "cash_increase",
}
