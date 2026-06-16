"""模拟盘 CLI — python -m quant.paper list/create/run/show/nav/trades"""

from __future__ import annotations

import argparse
from datetime import date

from db import Connection
from quant.paper.engine import PaperTradingEngine


def _f(v, default=0):
    return float(v) if v is not None else default


def _serialize_row(cur, row) -> dict:
    if row is None:
        return None
    return dict(zip((d[0] for d in cur.description), row))


def cmd_list(args):
    with Connection() as conn:
        cur = conn.cursor()
        sql = "SELECT * FROM paper_accounts"
        params: list = []
        if args.status:
            sql += " WHERE status = %s"
            params.append(args.status)
        sql += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([args.limit, args.offset])
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        cur.close()
    if not rows:
        print("(no accounts)")
        return
    for r in rows:
        d = dict(zip(cols, r))
        print(
            f"  {d['account_id'][:12]}  {d['account_name']:<20s}  "
            f"{d['strategy_name']:<20s}  nav={_f(d['nav']):.4f}  "
            f"value={_f(d['total_value']):,.0f}  {d['status']}"
        )


def cmd_create(args):
    import uuid
    from quant.screener.presets import COMPOSITE_PRESETS

    account_id = uuid.uuid4().hex[:32]
    preset_type = "composite" if args.strategy in COMPOSITE_PRESETS else "normal"

    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO paper_accounts
               (account_id, account_name, strategy_name, preset_type, market,
                benchmark, initial_capital, cash, total_value, nav,
                fee_rate, slippage_bps, config)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               RETURNING account_id""",
            (
                account_id, args.name, args.strategy, preset_type,
                args.market, args.benchmark,
                args.capital, args.capital, args.capital, 1.0,
                args.fee, args.slippage, "{}",
            ),
        )
        conn.commit()
        row = cur.fetchone()
        cur.close()
    print(f"Created account: {row[0]} (preset_type={preset_type})")


def cmd_run(args):
    engine = PaperTradingEngine(args.account_id)
    as_of = date.fromisoformat(args.date) if args.date else None
    result = engine.run(as_of)
    if result["status"] == "skipped":
        print(f"Skipped: already ran on {result['run_date']}")
        return
    print(f"Run: {result['run_type']} on {result['run_date']}")
    if result["signals"]:
        print(f"  Signals: {result['signals']}")
    if result["allocation"]:
        print(f"  Allocation: {result['allocation']}")
    if result["trades"]:
        print(f"  Trades: {len(result['trades'])}")
        for t in result["trades"]:
            print(f"    {t['side']} {t['stock_code']} {t['shares']:.0f}@{t['price']:.2f}")
    if result["nav_after"]:
        na = result["nav_after"]
        print(f"  NAV: {na['nav']:.4f}  total_value={na['total_value']:,.0f}  "
              f"cash={na['cash']:,.0f}  positions={na['position_count']}")


def cmd_show(args):
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM paper_accounts WHERE account_id=%s", (args.account_id,))
        row = cur.fetchone()
        if not row:
            print(f"Account {args.account_id} not found")
            return
        cols = [d[0] for d in cur.description]
        a = dict(zip(cols, row))
        cur.close()

        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM paper_positions WHERE account_id=%s ORDER BY sub_strategy, stock_code",
            (args.account_id,),
        )
        holdings = [_serialize_row(cur, r) for r in cur.fetchall()]
        cur.close()

        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM paper_trades WHERE account_id=%s ORDER BY trade_date DESC LIMIT 20",
            (args.account_id,),
        )
        trades = [_serialize_row(cur, r) for r in cur.fetchall()]
        cur.close()

        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM paper_strategy_runs WHERE account_id=%s ORDER BY run_date DESC LIMIT 5",
            (args.account_id,),
        )
        runs = [_serialize_row(cur, r) for r in cur.fetchall()]
        cur.close()

    print(f"Account: {a['account_name']} ({a['account_id'][:12]})")
    print(f"  Strategy: {a['strategy_name']}  Market: {a['market']}  Status: {a['status']}")
    print(f"  NAV: {_f(a['nav']):.4f}  Value: {_f(a['total_value']):,.0f}  "
          f"Cash: {_f(a['cash']):,.0f}  Capital: {_f(a['initial_capital']):,.0f}")
    print(f"  Fee: {_f(a['fee_rate'])*100:.2f}%  Slippage: {_f(a['slippage_bps'])}bps")
    if a["last_valued_at"]:
        print(f"  Last valued: {a['last_valued_at']}")

    print(f"\n  Holdings ({len(holdings)}):")
    for h in holdings:
        sub = h["sub_strategy"] or ""
        print(f"    [{sub}] {h['stock_code']}  shares={_f(h['shares']):.0f}  "
              f"cost={_f(h['avg_cost']):.2f}  price={_f(h['last_price'] or 0):.2f}  "
              f"mv={_f(h['market_value']):,.0f}")

    print(f"\n  Recent trades ({len(trades)}):")
    for t in trades:
        print(f"    {t['trade_date']} {t['side']} {t['stock_code']} "
              f"shares={_f(t['shares']):.0f}@{_f(t['price']):.2f}")

    print(f"\n  Recent runs ({len(runs)}):")
    for r in runs:
        print(f"    {r['run_date']} {r['run_type']} -> {r['status']}")


def cmd_nav(args):
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT value_date, nav, benchmark_nav, cash, market_value, total_value,
                      daily_return, drawdown, position_count
               FROM paper_nav_snapshots
               WHERE account_id=%s ORDER BY value_date DESC LIMIT %s""",
            (args.account_id, args.days),
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        cur.close()
    if not rows:
        print("(no nav snapshots)")
        return
    for r in rows:
        d = dict(zip(cols, r))
        dr = f" ({_f(d['daily_return'])*100:+.2f}%)" if d["daily_return"] is not None else ""
        dd = f" dd={_f(d['drawdown'])*100:.1f}%" if d["drawdown"] is not None else ""
        print(
            f"  {d['value_date']}  nav={_f(d['nav']):.4f}{dr}{dd}  "
            f"value={_f(d['total_value']):,.0f}  pos={d['position_count']}"
        )


def main():
    parser = argparse.ArgumentParser(description="Paper Trading CLI")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list")
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--offset", type=int, default=0)

    p_create = sub.add_parser("create")
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--strategy", default="commodity_rotation")
    p_create.add_argument("--market", default="CN_A", choices=["CN_A", "CN_HK", "US"])
    p_create.add_argument("--capital", type=float, default=1_000_000)
    p_create.add_argument("--benchmark", default="000300")
    p_create.add_argument("--fee", type=float, default=0.0)
    p_create.add_argument("--slippage", type=float, default=0.0)

    p_run = sub.add_parser("run")
    p_run.add_argument("account_id")
    p_run.add_argument("--date", default=None)

    p_show = sub.add_parser("show")
    p_show.add_argument("account_id")

    p_nav = sub.add_parser("nav")
    p_nav.add_argument("account_id")
    p_nav.add_argument("--days", type=int, default=90)

    args = parser.parse_args()
    if args.command == "list":
        cmd_list(args)
    elif args.command == "create":
        cmd_create(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "show":
        cmd_show(args)
    elif args.command == "nav":
        cmd_nav(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
