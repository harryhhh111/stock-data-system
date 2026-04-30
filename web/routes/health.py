"""Health check endpoint."""
from fastapi import APIRouter

from web import ok, err

router = APIRouter()


@router.get("/health")
async def health_check():
    """DB 连接检查。"""
    try:
        from db import Connection
        with Connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
        return ok({"db": True})
    except Exception as e:
        return err("db_unreachable", str(e))
