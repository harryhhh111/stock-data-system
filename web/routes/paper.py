"""模拟盘 API 端点。"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from web import ok, err
from web.services import paper_service

router = APIRouter()


class CreateAccountParams(BaseModel):
    account_name: str
    strategy_name: str
    preset_type: str = "composite"
    market: str = "CN_A"
    benchmark: str | None = None
    initial_capital: float = 1_000_000
    fee_rate: float = 0.0
    slippage_bps: float = 0.0
    config: dict = Field(default_factory=dict)


class RunAccountParams(BaseModel):
    as_of_date: str | None = None


@router.get("/paper/accounts")
def list_accounts(
    status: str | None = Query(None),
    limit: int = Query(50),
    offset: int = Query(0),
):
    return ok(paper_service.list_accounts(status, limit, offset))


@router.post("/paper/accounts")
def create_account(params: CreateAccountParams):
    try:
        account = paper_service.create_account(params.model_dump())
        return ok(account)
    except ValueError as e:
        return err("create_error", str(e))


@router.get("/paper/accounts/{account_id}")
def get_account(account_id: str):
    detail = paper_service.get_account_detail(account_id)
    if detail is None:
        return err("not_found", f"Account {account_id} not found")
    return ok(detail)


@router.post("/paper/accounts/{account_id}/run")
def run_account(account_id: str, params: RunAccountParams | None = None):
    try:
        as_of_date = params.as_of_date if params else None
        result = paper_service.run_account(account_id, as_of_date)
        return ok(result)
    except ValueError as e:
        return err("run_error", str(e))


@router.get("/paper/accounts/{account_id}/nav")
def get_nav_history(account_id: str, days: int = Query(90)):
    return ok(paper_service.get_nav_history(account_id, days))


@router.get("/paper/accounts/{account_id}/trades")
def get_trades(account_id: str, limit: int = Query(100), offset: int = Query(0)):
    return ok(paper_service.get_trades(account_id, limit, offset))
