"""tests/test_fact_version_identity.py

解析后事实版本身份回归(2026-08-13 唯一键 +standard_field 迁移):
同一份原始 XBRL 事实允许"旧错误分类(被排除)"与"新正确分类"两行并存。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.us_integration

from db import Connection

_STOCK = "ZZTEST_IDENT"
_ACCN = "0000000000-26-999999"


def _rec(field: str, value: float = 100.0) -> dict:
    return {
        "accn": _ACCN, "end": "2025-12-31", "val": value,
        "start": "2025-01-01", "fp": "FY", "fy": 2025, "form": "10-K",
        "filed": "2026-02-20", "frame": None, "unit": "USD",
        "tag": "ProfitLoss", "field": field, "dimensions": {},
        "_period_kind": "duration",
    }


def _real_snapshot_id() -> int:
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT snapshot_id FROM raw_snapshot_version ORDER BY snapshot_id LIMIT 1")
        return cur.fetchone()[0]


@pytest.fixture
def cleanup():
    yield
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM us_financial_fact_source WHERE fact_version_id IN "
                    "(SELECT fact_version_id FROM us_financial_fact_version WHERE stock_code=%s)", (_STOCK,))
        cur.execute("DELETE FROM us_financial_fact_version WHERE stock_code=%s", (_STOCK,))
        cur.execute("DELETE FROM us_filing WHERE accession_no=%s", (_ACCN,))
        conn.commit()


def test_same_raw_fact_two_classifications_coexist(cleanup):
    """同一 raw fact:operating_income 行与 net_income 行可同时写入、各自可追踪。"""
    from core.us_financial_versioning import USFactVersionWriter
    from types import SimpleNamespace

    ctx = SimpleNamespace(stock_code=_STOCK, cik="0000000000",
                          snapshot_id=_real_snapshot_id())
    writer = USFactVersionWriter()

    with Connection() as conn:
        r1 = writer.write_facts(conn, ctx, None, [_rec("operating_income")], [], "income")
        conn.commit()
        assert r1["facts_inserted"] == 1

    with Connection() as conn:
        # 同 raw fact、不同 standard_field:必须作为新行写入,不得判为 repeated
        r2 = writer.write_facts(conn, ctx, None, [_rec("net_income")], [], "income")
        conn.commit()
        assert r2["facts_inserted"] == 1

    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT standard_field, value_numeric FROM us_financial_fact_version "
            "WHERE stock_code=%s AND accession_no=%s ORDER BY standard_field",
            (_STOCK, _ACCN),
        )
        rows = cur.fetchall()
        assert [(r[0], float(r[1])) for r in rows] == [
            ("net_income", 100.0), ("operating_income", 100.0)]

    with Connection() as conn:
        # 完全相同(同 field 同值)仍应判为 repeated,不得重复插入
        r3 = writer.write_facts(conn, ctx, None, [_rec("net_income")], [], "income")
        conn.commit()
        assert r3["facts_inserted"] == 0
        assert r3["facts_repeated"] == 1
