"""Sync service — 同步状态与日志。"""
from db import Connection


def get_status(market: str | None) -> dict:
    """同步进度摘要（市场级），返回 SyncStatusByMarket[]。"""
    with Connection() as conn:
        cur = conn.cursor()

        if market:
            cur.execute(
                "SELECT market, status, COUNT(*) FROM sync_progress WHERE market=%s GROUP BY market, status",
                (market,),
            )
        else:
            cur.execute(
                "SELECT market, status, COUNT(*) FROM sync_progress GROUP BY market, status"
            )

        markets: dict[str, dict] = {}
        for m, st, cnt in cur.fetchall():
            if m not in markets:
                markets[m] = {
                    "market": m,
                    "total_stocks": 0,
                    "success": 0,
                    "failed": 0,
                    "in_progress": 0,
                    "partial": 0,
                    "last_sync_time": None,
                    "last_report_date": None,
                }
            markets[m][st] = cnt
            markets[m]["total_stocks"] += cnt

        # 补充 last_sync_time 和 last_report_date
        for m in markets:
            cur.execute(
                "SELECT MAX(last_sync_time), MAX(last_report_date) FROM sync_progress WHERE market=%s",
                (m,),
            )
            row = cur.fetchone()
            if row[0]:
                markets[m]["last_sync_time"] = row[0].isoformat()
            if row[1]:
                markets[m]["last_report_date"] = row[1].isoformat()

        cur.close()

    return list(markets.values())


def get_progress(market: str | None, limit: int, offset: int) -> dict:
    """个股同步进度，返回 Paginated<SyncProgressEntry>。"""
    with Connection() as conn:
        cur = conn.cursor()

        where = f"WHERE sp.market = %s" if market else ""
        params = [market, limit, offset] if market else [limit, offset]

        cur.execute(
            f"""
            SELECT sp.stock_code, si.stock_name, sp.market, sp.status, sp.tables_synced,
                   sp.last_sync_time, sp.last_report_date, sp.error_detail
            FROM sync_progress sp
            LEFT JOIN stock_info si ON sp.stock_code = si.stock_code
            {where}
            ORDER BY sp.last_sync_time DESC NULLS LAST
            LIMIT %s OFFSET %s
            """,
            params,
        )
        items = []
        for row in cur.fetchall():
            items.append({
                "stock_code": row[0],
                "stock_name": row[1],
                "market": row[2],
                "status": row[3],
                "tables_synced": row[4] or [],
                "last_sync_time": row[5].isoformat() if row[5] else None,
                "last_report_date": row[6].isoformat() if row[6] else None,
                "error_detail": row[7],
            })

        # 总数
        cur.execute(
            f"SELECT COUNT(*) FROM sync_progress sp {where.replace('sp.market', 'sp.market')}",
            params[:1] if market else [],
        )
        total = cur.fetchone()[0]
        cur.close()

    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_log(market: str | None, limit: int, offset: int) -> dict:
    """同步日志历史，返回 Paginated<SyncLogEntry>。"""
    with Connection() as conn:
        cur = conn.cursor()

        where = "WHERE config_json->>'market' = %s" if market else ""
        params = [market, limit, offset] if market else [limit, offset]

        cur.execute(
            f"""
            SELECT id, data_type, config_json->>'market' AS market, status,
                   to_char(started_at, 'YYYY-MM-DD HH24:MI:SS'),
                   to_char(finished_at, 'YYYY-MM-DD HH24:MI:SS'),
                   success_count, fail_count,
                   EXTRACT(EPOCH FROM finished_at - started_at) AS elapsed_seconds,
                   error_detail
            FROM sync_log
            {where}
            ORDER BY started_at DESC
            LIMIT %s OFFSET %s
            """,
            params,
        )
        items = []
        for row in cur.fetchall():
            items.append({
                "id": row[0],
                "data_type": row[1],
                "market": row[2] or "",
                "status": row[3],
                "started_at": row[4],
                "finished_at": row[5],
                "success_count": row[6],
                "fail_count": row[7],
                "elapsed_seconds": round(row[8], 1) if row[8] else None,
                "error_detail": row[9],
            })

        cur.execute(f"SELECT COUNT(*) FROM sync_log {where}", params[:1] if market else [])
        total = cur.fetchone()[0]
        cur.close()

    return {"items": items, "total": total, "limit": limit, "offset": offset}
