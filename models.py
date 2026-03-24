"""
数据模型 - 使用SQLAlchemy + Pydantic
宽表格式：每行一张报表，列 = 科目
"""
from datetime import datetime
from typing import Optional, List
from sqlalchemy import create_engine, Column, String, Float, Integer, Date, DateTime, Text, Index, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool
from pydantic import BaseModel, Field
import config

Base = declarative_base()

# ============================================================
# SQLAlchemy 模型（对应数据库表）
# ============================================================

class StockInfo(Base):
    """股票基本信息"""
    __tablename__ = "stock_info"
    
    stock_code = Column(String(10), primary_key=True)  # 股票代码，如 "600000"
    stock_name = Column(String(100), nullable=False)   # 股票名称
    market = Column(String(10), nullable=False)        # 市场：CN_A（中国A股）、HK（港股）
    exchange = Column(String(20))                        # 交易所：shanghai、shenzhen、hk
    industry = Column(String(100))                      # 所属行业
    list_date = Column(Date)                            # 上市日期
    is_hs = Column(String(10))                          # 是否沪深港通：HS（是）、NH（非）
    update_time = Column(DateTime, default=datetime.now)
    
    __table_args__ = (
        Index("idx_market", "market"),
        Index("idx_industry", "industry"),
    )


class FinancialIndicator(Base):
    """财务指标（周更数据）"""
    __tablename__ = "financial_indicator"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    stock_code = Column(String(10), nullable=False)
    indicator_date = Column(Date, nullable=False)       # 指标日期（周线）
    
    # 基本指标
    pe = Column(Float)                                   # 市盈率
    pb = Column(Float)                                   # 市净率
    ps = Column(Float)                                   # 市销率
    pcf = Column(Float)                                  # 市现率
    
    # 估值指标
    market_cap = Column(Float)                           # 总市值
    float_market_cap = Column(Float)                     # 流通市值
    
    # 盈利能力
    roe = Column(Float)                                 # 净资产收益率
    gross_margin = Column(Float)                         # 毛利率
    net_margin = Column(Float)                           # 净利率
    
    # 成长能力
    revenue_growth = Column(Float)                       # 营收增长率
    profit_growth = Column(Float)                       # 利润增长率
    
    # 财务安全
    debt_ratio = Column(Float)                           # 资产负债率
    current_ratio = Column(Float)                       # 流动比率
    
    # 营运能力
    turnover_rate = Column(Float)                        # 换手率
    
    # 现金流指标
    fcf = Column(Float)                                  # 自由现金流 = 经营现金流 - 资本支出
    fcf_yield = Column(Float)                            # FCF收益率 = FCF / 市值
    
    update_time = Column(DateTime, default=datetime.now)
    
    __table_args__ = (
        UniqueConstraint("stock_code", "indicator_date", name="uq_financial_indicator"),
    )


class IncomeStatement(Base):
    """利润表（季报）"""
    __tablename__ = "income_statement"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    stock_code = Column(String(10), nullable=False)
    report_date = Column(String(7), nullable=False)     # 报告期，格式 "2024-Q1"
    
    # 收入
    revenue = Column(Float)                              # 营业收入
    revenue_yoy = Column(Float)                          # 营收同比
    
    # 成本
    cost = Column(Float)                                 # 营业成本
    gross_profit = Column(Float)                        # 毛利润
    
    # 费用
    selling_expense = Column(Float)                     # 销售费用
    managing_expense = Column(Float)                     # 管理费用
    rd_expense = Column(Float)                          # 研发费用
    financial_expense = Column(Float)                    # 财务费用
    
    # 利润
    operating_profit = Column(Float)                    # 营业利润
    operating_profit_yoy = Column(Float)                 # 营业利润同比
    total_profit = Column(Float)                        # 利润总额
    net_profit = Column(Float)                          # 净利润
    net_profit_yoy = Column(Float)                      # 净利润同比
    attr_profit = Column(Float)                         # 归母净利润
    
    # 每股收益
    eps = Column(Float)                                 # 每股收益
    
    update_time = Column(DateTime, default=datetime.now)
    
    __table_args__ = (
        UniqueConstraint("stock_code", "report_date", name="uq_income_statement"),
    )


class BalanceSheet(Base):
    """资产负债表（季报）"""
    __tablename__ = "balance_sheet"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    stock_code = Column(String(10), nullable=False)
    report_date = Column(String(7), nullable=False)
    
    # 资产
    total_assets = Column(Float)                        # 资产总计
    current_assets = Column(Float)                      # 流动资产
    fixed_assets = Column(Float)                        # 固定资产
    
    # 负债
    total_liabilities = Column(Float)                   # 负债合计
    current_liabilities = Column(Float)                # 流动负债
    longterm_liabilities = Column(Float)               # 长期负债
    
    # 所有者权益
    total_equity = Column(Float)                       # 所有者权益合计
    attr_equity = Column(Float)                       # 归母权益
    
    # 补充
    goodwill = Column(Float)                           # 商誉
    
    update_time = Column(DateTime, default=datetime.now)
    
    __table_args__ = (
        UniqueConstraint("stock_code", "report_date", name="uq_balance_sheet"),
    )


class CashFlowStatement(Base):
    """现金流量表（季报）"""
    __tablename__ = "cash_flow_statement"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    stock_code = Column(String(10), nullable=False)
    report_date = Column(String(7), nullable=False)
    
    # 经营活动
    operating_cash_flow = Column(Float)                # 经营活动现金流量
    operating_cash_flow_yoy = Column(Float)           # 经营现金流同比
    
    # 投资活动
    investing_cash_flow = Column(Float)               # 投资活动现金流量
    
    # 筹资活动
    financing_cash_flow = Column(Float)               # 筹资活动现金流量
    
    # 净现金流
    net_cash_flow = Column(Float)                      # 现金及等价物净增加额
    cash_balance = Column(Float)                       # 期末现金余额
    
    update_time = Column(DateTime, default=datetime.now)
    
    __table_args__ = (
        UniqueConstraint("stock_code", "report_date", name="uq_cash_flow_statement"),
    )


class IndexConstituent(Base):
    """指数成分股"""
    __tablename__ = "index_constituent"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    index_code = Column(String(20), nullable=False)   # 指数代码，如 "000300"（沪深300）
    index_name = Column(String(100))                   # 指数名称
    stock_code = Column(String(10), nullable=False)    # 成分股代码
    stock_name = Column(String(100))                   # 成分股名称
    effective_date = Column(Date)                      # 生效日期
    expiry_date = Column(Date)                         # 失效日期（NULL表示当前有效）
    in_date = Column(Date)                             # 纳入日期
    out_date = Column(Date)                            # 剔除日期（NULL表示仍在）
    is_active = Column(Integer, default=1)            # 是否在位：1是，0否
    
    update_time = Column(DateTime, default=datetime.now)
    
    __table_args__ = (
        UniqueConstraint("index_code", "stock_code", "effective_date", name="uq_index_constituent"),
        Index("idx_index", "index_code"),
        Index("idx_stock", "stock_code"),
    )


class Dividend(Base):
    """分红记录"""
    __tablename__ = "dividend"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    stock_code = Column(String(10), nullable=False)
    report_date = Column(String(7), nullable=False)     # 报告期，如 "2024-Q1"
    dividend_per_share = Column(Float)                  # 每股分红（元）
    record_date = Column(Date)                          # 股权登记日
    ex_date = Column(Date)                              # 除权除息日
    pay_date = Column(Date)                             # 派息日
    dividend_type = Column(String(20))                  # 分红类型：cash（现金）、stock（送股）
    
    update_time = Column(DateTime, default=datetime.now)
    
    __table_args__ = (
        UniqueConstraint("stock_code", "report_date", "dividend_type", "ex_date", name="uq_dividend"),
    )


class Split(Base):
    """拆股配股记录"""
    __tablename__ = "split"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    stock_code = Column(String(10), nullable=False)
    report_date = Column(String(7), nullable=False)     # 报告期
    ex_date = Column(Date, nullable=False)              # 除权日
    split_ratio = Column(Float)                         # 拆股比例（如 2 表示1拆2）
    bonus_ratio = Column(Float)                         # 送股比例（每10股送几股）
    convert_ratio = Column(Float)                       # 配股比例（每10股配几股）
    convert_price = Column(Float)                       # 配股价
    
    update_time = Column(DateTime, default=datetime.now)
    
    __table_args__ = (
        UniqueConstraint("stock_code", "ex_date", name="uq_split"),
    )


class SyncLog(Base):
    """数据同步日志"""
    __tablename__ = "sync_log"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    sync_type = Column(String(50), nullable=False)      # 同步类型：stock_list、financial_data、index_constituent
    status = Column(String(20), nullable=False)         # 状态：success、failed、partial
    start_time = Column(DateTime, nullable=False)       # 开始时间
    end_time = Column(DateTime)                         # 结束时间
    records_synced = Column(Integer, default=0)         # 同步记录数
    records_failed = Column(Integer, default=0)         # 失败记录数
    error_message = Column(Text)                        # 错误信息
    
    __table_args__ = (
        Index("idx_sync_type", "sync_type"),
        Index("idx_sync_time", "start_time"),
    )


# ============================================================
# Pydantic 模型（用于API响应）
# ============================================================

class StockInfoResponse(BaseModel):
    stock_code: str
    stock_name: str
    market: str
    exchange: Optional[str] = None
    industry: Optional[str] = None
    list_date: Optional[datetime] = None
    is_hs: Optional[str] = None


class FinancialIndicatorResponse(BaseModel):
    stock_code: str
    indicator_date: datetime
    pe: Optional[float] = None
    pb: Optional[float] = None
    roe: Optional[float] = None
    gross_margin: Optional[float] = None
    net_margin: Optional[float] = None
    revenue_growth: Optional[float] = None
    profit_growth: Optional[float] = None


class IncomeStatementResponse(BaseModel):
    stock_code: str
    report_date: str
    revenue: Optional[float] = None
    net_profit: Optional[float] = None
    attr_profit: Optional[float] = None
    eps: Optional[float] = None


# ============================================================
# 数据库初始化
# ============================================================

def get_engine():
    """获取数据库引擎"""
    engine = create_engine(
        f"sqlite:///{config.DB_PATH}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    return engine


def init_db():
    """初始化数据库表"""
    engine = get_engine()
    Base.metadata.create_all(engine)
    return engine


def get_session():
    """获取数据库会话"""
    engine = get_engine()
    Session = sessionmaker(bind=engine)
    return Session()
