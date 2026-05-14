"""回测数据预加载 — 一次加载，内存 PIT 过滤。"""

from __future__ import annotations

import io
from datetime import date

import pandas as pd
from db import Connection


def _copy_df(conn, sql: str, str_cols: tuple[str, ...] = ()) -> pd.DataFrame:
    """COPY CSV 快速加载，str_cols 指定需要强制字符串的列。"""
    buf = io.StringIO()
    cur = conn.cursor()
    cur.copy_expert(f"COPY ({sql}) TO STDOUT WITH CSV HEADER", buf)
    cur.close()
    buf.seek(0)
    dtype = {c: str for c in str_cols}
    df = pd.read_csv(buf, dtype=dtype or None)
    buf.close()
    return df


class PITPreloader:
    """一次性加载财务 / TTM / 股本 / 信息到内存，pandas 做 PIT。"""

    def __init__(self, market: str) -> None:
        if market not in ("CN_A", "CN_HK", "US"):
            raise ValueError(f"仅支持 CN_A / CN_HK / US，收到: {market}")
        self.market = market

    def load(self) -> None:
        """加载并预排序全部静态数据。"""
        if self.market == "US":
            return self._load_us()
        return self._load_cn()

    def _load_cn(self) -> None:
        with Connection() as conn:
            self.fin = _copy_df(
                conn,
                "SELECT stock_code, report_date, report_type, notice_date,"
                "  roe, gross_margin, operating_margin, net_margin,"
                "  debt_ratio, current_ratio, quick_ratio,"
                "  parent_equity, total_equity, total_assets, total_liab,"
                "  eps_basic, eps_diluted, revenue_yoy, net_profit_yoy, fcf"
                " FROM mv_financial_indicator"
                " WHERE report_type IN ('annual', 'quarterly')"
                "  AND notice_date >= '2015-01-01'",
                str_cols=("stock_code", "report_type"),
            )
            self.fin["notice_date"] = pd.to_datetime(
                self.fin["notice_date"], errors="coerce"
            ).dt.date
            self.fin["report_date"] = pd.to_datetime(
                self.fin["report_date"], errors="coerce"
            ).dt.date
            self.fin = self.fin.sort_values(
                ["stock_code", "report_date"], ascending=[True, False]
            )

            self.ttm = _copy_df(
                conn,
                "SELECT * FROM mv_indicator_ttm_hist",
                str_cols=("stock_code", "report_type"),
            )
            self.ttm["notice_date"] = pd.to_datetime(
                self.ttm["notice_date"], errors="coerce"
            ).dt.date
            self.ttm["report_date"] = pd.to_datetime(
                self.ttm["report_date"], errors="coerce"
            ).dt.date
            self.ttm = self.ttm.sort_values(
                ["stock_code", "report_date"], ascending=[True, False]
            )

            self.shares = _copy_df(
                conn,
                "SELECT stock_code, trade_date, total_shares FROM stock_share"
                f" WHERE market = '{self.market}'",
                str_cols=("stock_code",),
            )
            self.shares["trade_date"] = pd.to_datetime(
                self.shares["trade_date"], errors="coerce"
            ).dt.date
            self.shares = self.shares.sort_values(
                ["stock_code", "trade_date"], ascending=[True, False]
            )

            self.info = _copy_df(
                conn,
                f"SELECT stock_code, stock_name, market, industry, list_date"
                f" FROM stock_info WHERE market = '{self.market}'",
                str_cols=("stock_code", "stock_name", "industry", "market"),
            )
            self.info["list_date"] = pd.to_datetime(
                self.info["list_date"], errors="coerce"
            ).dt.date

    def _load_us(self) -> None:
        with Connection() as conn:
            self.us_fin = _copy_df(
                conn,
                "SELECT stock_code, report_date, report_type, filed_date,"
                "  roe, gross_margin, operating_margin, net_margin,"
                "  debt_ratio, current_ratio, quick_ratio,"
                "  total_equity, total_assets, total_liab,"
                "  eps_basic, eps_diluted, revenue_yoy, net_profit_yoy, fcf"
                " FROM mv_us_financial_indicator"
                " WHERE report_type IN ('annual', 'quarterly')"
                "  AND filed_date >= '2015-01-01'",
                str_cols=("stock_code", "report_type"),
            )
            for col in ("filed_date", "report_date"):
                self.us_fin[col] = pd.to_datetime(
                    self.us_fin[col], errors="coerce"
                ).dt.date
            self.us_fin = self.us_fin.sort_values(
                ["stock_code", "report_date"], ascending=[True, False]
            )

            self.us_income = _copy_df(
                conn,
                "SELECT stock_code, report_date, report_type, filed_date,"
                "  revenues, net_income"
                " FROM us_income_statement"
                " WHERE report_type IN ('annual', 'quarterly')"
                "  AND filed_date >= '2015-01-01'",
                str_cols=("stock_code", "report_type"),
            )
            for col in ("filed_date", "report_date"):
                self.us_income[col] = pd.to_datetime(
                    self.us_income[col], errors="coerce"
                ).dt.date
            self.us_income = self.us_income.sort_values(
                ["stock_code", "report_date"], ascending=[True, False]
            )

            self.us_cf = _copy_df(
                conn,
                "SELECT stock_code, report_date, report_type, filed_date,"
                "  net_cash_from_operations, capital_expenditures"
                " FROM us_cash_flow_statement"
                " WHERE report_type IN ('annual', 'quarterly')"
                "  AND filed_date >= '2015-01-01'",
                str_cols=("stock_code", "report_type"),
            )
            for col in ("filed_date", "report_date"):
                self.us_cf[col] = pd.to_datetime(
                    self.us_cf[col], errors="coerce"
                ).dt.date
            self.us_cf = self.us_cf.sort_values(
                ["stock_code", "report_date"], ascending=[True, False]
            )

            self.shares = _copy_df(
                conn,
                "SELECT stock_code, trade_date, total_shares FROM stock_share"
                " WHERE market = 'US'",
                str_cols=("stock_code",),
            )
            self.shares["trade_date"] = pd.to_datetime(
                self.shares["trade_date"], errors="coerce"
            ).dt.date
            self.shares = self.shares.sort_values(
                ["stock_code", "trade_date"], ascending=[True, False]
            )

            self.info = _copy_df(
                conn,
                "SELECT stock_code, stock_name, market, industry, list_date"
                " FROM stock_info WHERE market = 'US'",
                str_cols=("stock_code", "stock_name", "industry", "market"),
            )
            self.info["list_date"] = pd.to_datetime(
                self.info["list_date"], errors="coerce"
            ).dt.date

    # ── PIT 查询 ────────────────────────────────────────────

    def get_universe(self, as_of_date: date) -> pd.DataFrame:
        """内存构建 PIT 选股池的财务部分（不含行情）。"""
        if self.market == "US":
            return self._get_universe_us(as_of_date)
        return self._get_universe_cn(as_of_date)

    def _get_universe_cn(self, as_of_date: date) -> pd.DataFrame:
        annual = self.fin[
            (self.fin["report_type"] == "annual")
            & (self.fin["notice_date"] <= as_of_date)
        ]
        latest_annual = annual.drop_duplicates(subset="stock_code", keep="first")

        q = self.fin[
            (self.fin["report_type"] == "quarterly")
            & (self.fin["notice_date"] <= as_of_date)
            & self.fin["revenue_yoy"].notna()
        ]
        latest_q = q.drop_duplicates(subset="stock_code", keep="first")[
            ["stock_code", "revenue_yoy", "net_profit_yoy"]
        ]

        ttm_valid = self.ttm[self.ttm["notice_date"] <= as_of_date]
        latest_ttm = ttm_valid.drop_duplicates(subset="stock_code", keep="first")

        sh = self.shares[self.shares["trade_date"] <= as_of_date]
        latest_shares = sh.drop_duplicates(subset="stock_code", keep="first")

        result = self.info[
            ["stock_code", "stock_name", "market", "industry", "list_date"]
        ].copy()

        la_cols = [
            "stock_code", "roe", "gross_margin", "operating_margin", "net_margin",
            "debt_ratio", "current_ratio", "quick_ratio",
            "parent_equity", "total_equity", "total_assets", "total_liab",
            "eps_basic", "eps_diluted",
            "revenue_yoy", "net_profit_yoy", "fcf",
        ]
        result = result.merge(latest_annual[la_cols], on="stock_code", how="left")
        result = result.merge(
            latest_q, on="stock_code", how="left", suffixes=("", "_q")
        )
        result = result.merge(
            latest_ttm[
                ["stock_code", "revenue_ttm", "net_profit_ttm", "cfo_ttm",
                 "capex_ttm", "report_date"]
            ],
            on="stock_code", how="left",
        )
        result = result.merge(
            latest_shares[["stock_code", "total_shares"]],
            on="stock_code", how="left",
        )

        if "revenue_yoy_q" in result.columns:
            result["revenue_yoy"] = result["revenue_yoy"].fillna(
                result["revenue_yoy_q"]
            )
        if "net_profit_yoy_q" in result.columns:
            result["net_profit_yoy"] = result["net_profit_yoy"].fillna(
                result["net_profit_yoy_q"]
            )
        result = result.drop(
            columns=[c for c in result.columns if c.endswith("_q")],
            errors="ignore",
        )

        result["annual_fcf"] = result["fcf"]

        ld = pd.to_datetime(result["list_date"], errors="coerce")
        if ld.notna().any():
            result["days_since_list"] = (pd.Timestamp(as_of_date) - ld).dt.days
        else:
            result["days_since_list"] = None

        return result

    def _get_universe_us(self, as_of_date: date) -> pd.DataFrame:
        # latest annual from mv_us_financial_indicator
        fin_annual = self.us_fin[
            (self.us_fin["report_type"] == "annual")
            & (self.us_fin["filed_date"] <= as_of_date)
        ]
        latest_annual = fin_annual.drop_duplicates(
            subset="stock_code", keep="first"
        )

        # latest quarterly yoy
        fin_q = self.us_fin[
            (self.us_fin["report_type"] == "quarterly")
            & (self.us_fin["filed_date"] <= as_of_date)
            & self.us_fin["revenue_yoy"].notna()
        ]
        latest_q = fin_q.drop_duplicates(subset="stock_code", keep="first")[
            ["stock_code", "revenue_yoy", "net_profit_yoy"]
        ]

        # latest shares
        sh = self.shares[self.shares["trade_date"] <= as_of_date]
        latest_shares = sh.drop_duplicates(subset="stock_code", keep="first")

        # income TTM
        inc_ttm = self._compute_ttm(
            self.us_income, as_of_date, ["revenues", "net_income"]
        )
        cf_ttm = self._compute_ttm(
            self.us_cf, as_of_date,
            ["net_cash_from_operations", "capital_expenditures"],
        )

        result = self.info[
            ["stock_code", "stock_name", "market", "industry", "list_date"]
        ].copy()

        la_cols = [
            "stock_code", "roe", "gross_margin", "operating_margin", "net_margin",
            "debt_ratio", "current_ratio", "quick_ratio",
            "total_equity", "total_assets", "total_liab",
            "eps_basic", "eps_diluted",
            "revenue_yoy", "net_profit_yoy", "fcf",
        ]
        available = [c for c in la_cols if c in latest_annual.columns]
        result = result.merge(latest_annual[available], on="stock_code", how="left")

        # parent_equity alias for US (total_equity)
        if "total_equity" in result.columns:
            result["parent_equity"] = result["total_equity"]

        result = result.merge(
            latest_q, on="stock_code", how="left", suffixes=("", "_q")
        )
        if "revenue_yoy_q" in result.columns:
            result["revenue_yoy"] = result["revenue_yoy"].fillna(
                result["revenue_yoy_q"]
            )
        if "net_profit_yoy_q" in result.columns:
            result["net_profit_yoy"] = result["net_profit_yoy"].fillna(
                result["net_profit_yoy_q"]
            )
        result = result.drop(
            columns=[c for c in result.columns if c.endswith("_q")],
            errors="ignore",
        )

        if not inc_ttm.empty:
            inc_ttm = inc_ttm.rename(columns={
                "revenues": "revenue_ttm",
                "net_income": "net_profit_ttm",
            })
            result = result.merge(
                inc_ttm[["stock_code", "revenue_ttm", "net_profit_ttm"]],
                on="stock_code", how="left",
            )
        else:
            result["revenue_ttm"] = None
            result["net_profit_ttm"] = None

        if not cf_ttm.empty:
            cf_ttm = cf_ttm.rename(columns={
                "net_cash_from_operations": "cfo_ttm",
                "capital_expenditures": "capex_ttm",
            })
            result = result.merge(
                cf_ttm[["stock_code", "cfo_ttm", "capex_ttm"]],
                on="stock_code", how="left",
            )
        else:
            result["cfo_ttm"] = None
            result["capex_ttm"] = None

        result = result.merge(
            latest_shares[["stock_code", "total_shares"]],
            on="stock_code", how="left",
        )

        result["annual_fcf"] = result.get("fcf")
        result["report_date"] = None

        ld = pd.to_datetime(result["list_date"], errors="coerce")
        if ld.notna().any():
            result["days_since_list"] = (pd.Timestamp(as_of_date) - ld).dt.days
        else:
            result["days_since_list"] = None

        return result

    @staticmethod
    def _compute_ttm(
        df: pd.DataFrame, as_of_date: date, cols: list[str],
    ) -> pd.DataFrame:
        """计算 trailing twelve months（与 US PIT SQL 中的 CTE 逻辑一致）。"""
        valid = df[df["filed_date"] <= as_of_date].copy()
        if valid.empty:
            return pd.DataFrame()

        latest = valid.drop_duplicates(subset="stock_code", keep="first")

        # prev year: same report_type, closest to 1 year ago (±7 day window)
        merged = latest.merge(valid, on="stock_code", suffixes=("", "_py"))
        same_type = merged[merged["report_type"] == merged["report_type_py"]]
        rd = pd.to_datetime(same_type["report_date"])
        rd_py = pd.to_datetime(same_type["report_date_py"])
        diff = (rd - rd_py).dt.days
        window = same_type[(diff >= 358) & (diff <= 372)].copy()
        window["_abs"] = (diff[(diff >= 358) & (diff <= 372)] - 365).abs()
        prev_year = window.sort_values("_abs").drop_duplicates(
            subset="stock_code", keep="first"
        )

        # last annual
        annuals = valid[valid["report_type"] == "annual"]
        la_merged = latest.merge(annuals, on="stock_code", suffixes=("", "_la"))
        la_before = la_merged[
            la_merged["report_date_la"] < la_merged["report_date"]
        ]
        last_annual = la_before.sort_values(
            "report_date_la", ascending=False
        ).drop_duplicates(subset="stock_code", keep="first")

        # compute TTM
        result = latest[["stock_code"]].copy()
        is_annual = (latest.set_index("stock_code")["report_type"] == "annual")

        py_idx = prev_year.set_index("stock_code")
        la_idx = last_annual.set_index("stock_code")

        for col in cols:
            l_vals = latest.set_index("stock_code")[col]
            ttm = l_vals.copy()

            py_vals = py_idx.get(f"{col}_py")
            la_vals = la_idx.get(f"{col}_la")

            q_mask = ~is_annual
            if py_vals is not None and la_vals is not None:
                has_both = q_mask & la_vals.notna() & py_vals.notna()
                ttm[has_both] = l_vals[has_both] + la_vals[has_both] - py_vals[has_both]

                has_la_only = q_mask & ~has_both & la_vals.notna()
                ttm[has_la_only] = la_vals[has_la_only]
            elif la_vals is not None:
                has_la = q_mask & la_vals.notna()
                ttm[has_la] = la_vals[has_la]

            result[col] = result["stock_code"].map(ttm)

        return result

    def get_roe_history(self, as_of_date: date, years: int = 3) -> pd.DataFrame:
        """PIT 连续年 ROE。"""
        if self.market == "US":
            fin = self.us_fin
            date_col = "filed_date"
        else:
            fin = self.fin
            date_col = "notice_date"

        annual = fin[
            (fin["report_type"] == "annual")
            & fin["roe"].notna()
            & (fin[date_col] <= as_of_date)
        ]
        ranked = annual.copy()
        ranked["rn"] = ranked.groupby("stock_code").cumcount() + 1
        result = ranked[ranked["rn"] <= years][
            ["stock_code", "report_date", "roe"]
        ]
        return result.sort_values(
            ["stock_code", "report_date"], ascending=[True, False]
        )
