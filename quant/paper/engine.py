"""模拟盘引擎 — 每日估值 + 月度调仓。

PaperTradingEngine 是单日期运行的核心。它从 DB 加载状态，
调用 composite 引擎的函数做信号→分配→选股→调仓，
然后持久化结果。与回测引擎不同，它一次只处理一个交易日。
"""

from __future__ import annotations

import json
import logging
import uuid
from copy import deepcopy
from datetime import date, datetime

import pandas as pd

from db import Connection
from quant.backtest.common import (
    batch_query_quote,
    get_sell_prices_mixed,
    load_benchmark_prices,
    generate_rebalance_dates,
)
from quant.backtest.composite import (
    _check_all_signals,
    _allocate,
    _rebalance_sub_portfolio,
)
from quant.backtest.portfolio import Portfolio
from quant.backtest.universe import get_nearest_trade_date
from quant.screener.presets import COMPOSITE_PRESETS, CompositeConfig
from quant.paper.preloader import PaperPreloader

logger = logging.getLogger(__name__)


def _dict_row(cur, row) -> dict:
    if row is None:
        return None
    return dict(zip((d[0] for d in cur.description), row))


def _acct_float(account: dict, key: str) -> float:
    """从账户 dict 取数值字段，处理 DB 的 Decimal 类型。"""
    val = account.get(key, 0)
    return float(val) if val is not None else 0.0


class PaperTradingEngine:

    def __init__(self, account_id: str, conn: Connection | None = None) -> None:
        self.account_id = account_id
        self._conn = conn
        self._account: dict | None = None
        self._cfg: CompositeConfig | None = None

    # ── 数据加载 ────────────────────────────────────────

    def _load_account(self) -> dict:
        with _get_conn(self._conn) as c:
            cur = c.cursor()
            cur.execute(
                "SELECT * FROM paper_accounts WHERE account_id = %s",
                (self.account_id,),
            )
            row = cur.fetchone()
            cur.close()
            if not row:
                raise ValueError(f"Account {self.account_id} not found")
            self._account = _dict_row(cur, row)
        if self._account["status"] != "active":
            raise ValueError(
                f"Account {self.account_id} is not active (status={self._account['status']})"
            )
        return self._account

    def _load_config(self) -> CompositeConfig:
        strategy = self._account["strategy_name"]
        if strategy not in COMPOSITE_PRESETS:
            raise ValueError(
                f"Unknown composite strategy: {strategy}. "
                f"Available: {list(COMPOSITE_PRESETS.keys())}"
            )
        cfg = COMPOSITE_PRESETS[strategy]
        if cfg.get("type") != "composite":
            raise ValueError(f"{strategy} is not a composite strategy")
        self._cfg = cfg
        return cfg

    def _load_current_positions(self) -> dict[str, Portfolio]:
        sub_portfolios: dict[str, Portfolio] = {}
        with Connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM paper_positions WHERE account_id = %s",
                (self.account_id,),
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            cur.close()
        for r in rows:
            d = dict(zip(cols, r))
            sub = d.get("sub_strategy") or ""
            if sub not in sub_portfolios:
                sub_portfolios[sub] = Portfolio(0.0)
            pf = sub_portfolios[sub]
            code = d["stock_code"]
            shares = float(d["shares"])
            avg_cost = float(d["avg_cost"])
            pf.positions[code] = type("P", (), {"shares": shares, "avg_cost": avg_cost})()
            pf._total_trades = 0
        return sub_portfolios

    # ── 持久化 ──────────────────────────────────────────

    def _save_positions(
        self,
        sub_portfolios: dict[str, Portfolio],
        prices: dict[str, float | None],
    ) -> None:
        with Connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM paper_positions WHERE account_id = %s",
                (self.account_id,),
            )
            for sub_name, pf in sub_portfolios.items():
                for code, pos in pf.positions.items():
                    price = (prices.get(code) or pos.avg_cost) if prices else pos.avg_cost
                    market = self._account["market"]
                    mv = float(pos.shares) * float(price)
                    cur.execute(
                        """INSERT INTO paper_positions
                           (account_id, stock_code, market, sub_strategy, shares,
                            avg_cost, last_price, market_value, weight)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            self.account_id, code, market,
                            sub_name or None,
                            float(pos.shares), float(pos.avg_cost),
                            float(price), mv, 0.0,
                        ),
                    )
            conn.commit()
            cur.close()

    def _save_trades(
        self,
        old_portfolios: dict[str, Portfolio],
        new_portfolios: dict[str, Portfolio],
        trade_date: date,
        signals: dict[str, str],
        allocation: dict[str, float],
    ) -> list[dict]:
        trades: list[dict] = []
        signal_json = json.dumps(signals, default=str)
        fee_rate = _acct_float(self._account, "fee_rate")
        slippage_bps = _acct_float(self._account, "slippage_bps")

        all_subs = set(old_portfolios.keys()) | set(new_portfolios.keys())
        with Connection() as conn:
            cur = conn.cursor()
            for sub in all_subs:
                old_pf = old_portfolios.get(sub, Portfolio(0.0))
                new_pf = new_portfolios.get(sub, Portfolio(0.0))
                all_codes = set(old_pf.positions.keys()) | set(new_pf.positions.keys())
                for code in all_codes:
                    old_shares = old_pf.positions[code].shares if code in old_pf.positions else 0.0
                    new_shares = new_pf.positions[code].shares if code in new_pf.positions else 0.0
                    diff = new_shares - old_shares
                    if abs(diff) < 1e-9:
                        continue

                    side = "buy" if diff > 0 else "sell"
                    shares = abs(diff)
                    # 用调仓日价格近似成交价
                    price = float(new_pf.positions[code].avg_cost) if code in new_pf.positions else 0.0
                    amount = shares * price
                    fee = amount * fee_rate
                    slippage = amount * slippage_bps / 10000.0
                    reason = "rebalance"

                    cur.execute(
                        """INSERT INTO paper_trades
                           (account_id, trade_date, stock_code, market, sub_strategy,
                            side, shares, price, amount, fee, slippage, reason, signal_snapshot)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT DO NOTHING
                           RETURNING trade_id""",
                        (
                            self.account_id, trade_date, code, self._account["market"],
                            sub or None, side, shares, price, amount, fee, slippage,
                            reason, signal_json,
                        ),
                    )
                    row = cur.fetchone()
                    if row:
                        trades.append({
                            "trade_id": row[0],
                            "trade_date": str(trade_date),
                            "stock_code": code,
                            "market": self._account["market"],
                            "sub_strategy": sub or None,
                            "side": side,
                            "shares": shares,
                            "price": price,
                            "amount": amount,
                            "fee": fee,
                            "slippage": slippage,
                            "reason": reason,
                            "signal_snapshot": signals,
                        })
            conn.commit()
            cur.close()
        return trades

    def _save_nav_snapshot(
        self,
        value_date: date,
        sub_portfolios: dict[str, Portfolio],
        prices: dict[str, float | None],
        benchmark_nav_val: float | None,
    ) -> dict:
        total_cash = 0.0
        market_value = 0.0
        position_count = 0
        detail: dict[str, dict[str, float]] = {}
        for sub_name, pf in sub_portfolios.items():
            total_cash += float(pf.cash)
            sub_detail: dict[str, float] = {}
            for code, pos in pf.positions.items():
                price = (prices.get(code) or pos.avg_cost) if prices else float(pos.avg_cost)
                mv = float(pos.shares) * float(price)
                market_value += mv
                position_count += 1
                sub_detail[code] = {"shares": float(pos.shares), "price": price, "value": mv}
            detail[sub_name] = sub_detail

        total_value = total_cash + market_value
        nav = total_value / _acct_float(self._account, "initial_capital")

        # 计算日均收益率和回撤
        daily_return = None
        drawdown = None
        prev_nav = self._get_latest_nav(value_date)
        if prev_nav is not None and prev_nav > 0:
            daily_return = (nav - prev_nav) / prev_nav
        peak_nav = self._get_peak_nav(value_date)
        if peak_nav is not None and peak_nav > 0 and nav <= peak_nav:
            drawdown = (peak_nav - nav) / peak_nav

        with Connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO paper_nav_snapshots
                   (account_id, value_date, cash, market_value, total_value,
                    nav, benchmark_nav, daily_return, drawdown, position_count, snapshot)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (account_id, value_date) DO UPDATE SET
                     cash=EXCLUDED.cash, market_value=EXCLUDED.market_value,
                     total_value=EXCLUDED.total_value, nav=EXCLUDED.nav,
                     benchmark_nav=EXCLUDED.benchmark_nav,
                     daily_return=EXCLUDED.daily_return, drawdown=EXCLUDED.drawdown,
                     position_count=EXCLUDED.position_count, snapshot=EXCLUDED.snapshot,
                     updated_at=NOW()""",
                (
                    self.account_id, value_date, total_cash, market_value, total_value,
                    nav, benchmark_nav_val, daily_return, drawdown, position_count,
                    json.dumps(detail, default=str),
                ),
            )
            conn.commit()
            cur.close()
        return {
            "value_date": str(value_date),
            "cash": total_cash,
            "market_value": market_value,
            "total_value": total_value,
            "nav": nav,
            "benchmark_nav": benchmark_nav_val,
            "daily_return": daily_return,
            "drawdown": drawdown,
            "position_count": position_count,
        }

    def _get_latest_nav(self, before_date: date) -> float | None:
        with Connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT nav FROM paper_nav_snapshots
                   WHERE account_id=%s AND value_date < %s
                   ORDER BY value_date DESC LIMIT 1""",
                (self.account_id, before_date),
            )
            row = cur.fetchone()
            cur.close()
        return float(row[0]) if row else None

    def _get_peak_nav(self, before_date: date) -> float | None:
        with Connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT MAX(nav) FROM paper_nav_snapshots
                   WHERE account_id=%s AND value_date < %s""",
                (self.account_id, before_date),
            )
            row = cur.fetchone()
            cur.close()
        return float(row[0]) if row and row[0] is not None else None

    def _save_strategy_run(
        self,
        run_date: date,
        run_type: str,
        status: str,
        signals: dict,
        allocation: dict,
        target_positions: dict,
        trade_plan: dict,
        error_message: str | None,
    ) -> None:
        now = datetime.now()
        with Connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO paper_strategy_runs
                   (account_id, run_date, run_type, status, signals, allocation,
                    target_positions, trade_plan, error_message, started_at, finished_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (account_id, run_date, run_type) DO UPDATE SET
                     status=EXCLUDED.status, signals=EXCLUDED.signals,
                     allocation=EXCLUDED.allocation,
                     target_positions=EXCLUDED.target_positions,
                     trade_plan=EXCLUDED.trade_plan,
                     error_message=EXCLUDED.error_message,
                     finished_at=EXCLUDED.finished_at""",
                (
                    self.account_id, run_date, run_type, status,
                    json.dumps(signals, default=str),
                    json.dumps(allocation, default=str),
                    json.dumps(target_positions, default=str),
                    json.dumps(trade_plan, default=str),
                    error_message,
                    now, now,
                ),
            )
            conn.commit()
            cur.close()

    def _check_already_run(self, trade_date: date) -> dict | None:
        with Connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT run_type, status FROM paper_strategy_runs
                   WHERE account_id=%s AND run_date=%s AND status='success'
                   ORDER BY run_type DESC LIMIT 1""",
                (self.account_id, trade_date),
            )
            row = cur.fetchone()
            cur.close()
        if row:
            return {"run_type": row[0], "status": row[1]}
        return None

    def _update_account_summary(
        self, total_value: float, cash: float, nav: float, as_of_date: date
    ) -> None:
        now = datetime.now()
        with Connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE paper_accounts
                   SET total_value=%s, cash=%s, nav=%s, last_valued_at=%s, updated_at=%s
                   WHERE account_id=%s""",
                (total_value, cash, nav, now, now, self.account_id),
            )
            conn.commit()
            cur.close()

    def _is_rebalance_day(self, as_of_date: date) -> bool:
        market = self._account["market"]
        rb_dates = generate_rebalance_dates(
            as_of_date.replace(day=1), as_of_date, 1, market=market
        )
        return len(rb_dates) > 0 and rb_dates[-1] == as_of_date

    # ── 主入口 ──────────────────────────────────────────

    def run(self, as_of_date: date | None = None) -> dict:
        if as_of_date is None:
            as_of_date = date.today()

        account = self._load_account()
        cfg = self._load_config()
        market = account["market"]

        # 对齐到交易日
        trade_date = get_nearest_trade_date(as_of_date, market=market)
        if trade_date is None:
            raise ValueError(f"No trade data for {as_of_date} in {market}")

        # 幂等检查
        existing = self._check_already_run(trade_date)
        if existing is not None:
            logger.info("Already ran %s on %s, skipping", existing["run_type"], trade_date)
            return {
                "run_type": existing["run_type"],
                "run_date": str(trade_date),
                "status": "skipped",
                "signals": {},
                "allocation": {},
                "trades": [],
                "nav_before": None,
                "nav_after": None,
            }

        is_rebalance = self._is_rebalance_day(trade_date)

        # 加载当前持仓
        sub_portfolios = self._load_current_positions()
        old_portfolios = deepcopy(sub_portfolios) if is_rebalance else None

        # 获取当前行情
        all_codes = list({c for pf in sub_portfolios.values() for c in pf.positions})
        current_prices = get_sell_prices_mixed(trade_date, all_codes, account["benchmark"], market)

        # 获取基准价格
        benchmark_nav_val = None
        if account["benchmark"]:
            bp = load_benchmark_prices(account["benchmark"], market, trade_date, trade_date)
            if bp:
                benchmark_nav_val = list(bp.values())[0]
                # 按初始日归一化
                init_date = account.get("created_at")
                if init_date:
                    try:
                        init_date_val = date.fromisoformat(str(init_date)[:10])
                        init_bp = load_benchmark_prices(
                            account["benchmark"], market, init_date_val, init_date_val
                        )
                        if init_bp:
                            base = list(init_bp.values())[0]
                            if base > 0:
                                benchmark_nav_val = benchmark_nav_val / base
                    except Exception:
                        pass

        signals: dict[str, str] = {}
        allocation: dict[str, float] = {}
        trades: list[dict] = []

        if is_rebalance:
            signals = _check_all_signals(cfg, market, trade_date)
            allocation = _allocate(cfg, signals)

            preloader = PaperPreloader(market)
            with Connection() as conn:
                quote_by_date = batch_query_quote(conn, [trade_date], market)

            valuation_snaps = {sub["name"]: [] for sub in cfg["sub_strategies"]}

            for sub in cfg["sub_strategies"]:
                name = sub["name"]
                target_capital = _acct_float(account, "initial_capital") * allocation.get(name, 0.0)
                if name not in sub_portfolios:
                    sub_portfolios[name] = Portfolio(0.0)
                _rebalance_sub_portfolio(
                    sub=sub,
                    sub_pf=sub_portfolios[name],
                    name=name,
                    rb_date=trade_date,
                    target_capital=target_capital,
                    benchmark=account["benchmark"],
                    market=market,
                    preloader=preloader,
                    quote_by_date=quote_by_date,
                    signals=signals,
                    valuation_snaps=valuation_snaps,
                )

            trades = self._save_trades(old_portfolios or {}, sub_portfolios, trade_date, signals, allocation)

        # 估值
        nav_snap = self._save_nav_snapshot(trade_date, sub_portfolios, current_prices, benchmark_nav_val)

        # 持久化仓位
        self._save_positions(sub_portfolios, current_prices)

        # 计算汇总
        total_cash = sum(float(pf.cash) for pf in sub_portfolios.values())
        total_value = nav_snap["total_value"]
        nav_val = nav_snap["nav"]
        self._update_account_summary(total_value, total_cash, nav_val, trade_date)

        # 记录运行
        target_positions: dict[str, list[str]] = {}
        for sub_name, pf in sub_portfolios.items():
            target_positions[sub_name] = list(pf.positions.keys())

        trade_plan = {
            "buy": [t for t in trades if t["side"] == "buy"],
            "sell": [t for t in trades if t["side"] == "sell"],
        }
        self._save_strategy_run(
            run_date=trade_date,
            run_type="rebalance" if is_rebalance else "valuation",
            status="success",
            signals=signals,
            allocation=allocation,
            target_positions=target_positions,
            trade_plan=trade_plan,
            error_message=None,
        )

        return {
            "run_type": "rebalance" if is_rebalance else "valuation",
            "run_date": str(trade_date),
            "status": "success",
            "signals": signals,
            "allocation": allocation,
            "trades": trades,
            "nav_before": None,  # TODO: 从之前的快照中取
            "nav_after": nav_snap,
        }


from contextlib import contextmanager


@contextmanager
def _get_conn(maybe_conn: Connection | None = None):
    """获取连接：已有则复用，否则新建并自动关闭。"""
    if maybe_conn is not None:
        yield maybe_conn
    else:
        with Connection() as c:
            yield c
