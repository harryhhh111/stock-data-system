"""Read APIs plus fail-closed admin-authenticated watchlist writes."""
import logging
import os
import secrets
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from web import ok
from web.services import watchlist_service as service

router = APIRouter()
logger = logging.getLogger(__name__)
Market = Literal['US', 'CN_HK', 'CN_A']


def require_admin(authorization: str | None = Header(default=None)):
    token = os.environ.get('STOCK_WATCHLIST_ADMIN_TOKEN', '')
    if len(token) < 24:
        raise HTTPException(503, '请配置至少24字符的 STOCK_WATCHLIST_ADMIN_TOKEN 后启用关注列表编辑')
    supplied = authorization.removeprefix('Bearer ') if authorization else ''
    if not authorization or not authorization.startswith('Bearer ') or not secrets.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(401, '编辑凭据无效')


class ItemDetails(BaseModel):
    group_name: str = Field(default='', max_length=80)
    note: str = Field(default='', max_length=2000)
    research_url: str = Field(default='', max_length=2000)

    @field_validator('research_url')
    @classmethod
    def safe_url(cls, value):
        value = value.strip()
        parsed = urlparse(value)
        if value and (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password):
            raise ValueError('研究链接须为不含凭据的 HTTP(S) URL')
        return value


class NewItem(ItemDetails):
    market: Market
    stock_code: str = Field(min_length=1, max_length=20, pattern=r'^[A-Za-z0-9.\-]+$')
    stock_name: str = Field(min_length=1, max_length=200)

    @field_validator('stock_code')
    @classmethod
    def normalize_code(cls, value):
        return value.upper()

    @field_validator('stock_name')
    @classmethod
    def nonempty_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('公司名称不能为空')
        return value


class QuoteRequest(BaseModel):
    market: Market
    codes: list[str] = Field(min_length=1, max_length=200)

    @field_validator('codes')
    @classmethod
    def codes_valid(cls, codes):
        import re
        if any(not re.fullmatch(r'[A-Za-z0-9.\-]{1,20}', code) for code in codes):
            raise ValueError('无效股票代码')
        return list(dict.fromkeys(codes))


def call(operation, *args):
    try:
        return ok(operation(*args))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        logger.exception('watchlist operation failed: %s', getattr(operation, '__name__', type(operation).__name__))
        raise HTTPException(503, '关注股服务不可用，请检查数据库连接及 watchlist_tables.sql 迁移') from exc


@router.get('/watchlist')
def get_items():
    return call(service.list_items)


@router.post('/watchlist', dependencies=[Depends(require_admin)])
def create_item(item: NewItem):
    return call(service.save_item, item.model_dump())


@router.patch('/watchlist/{item_id}', dependencies=[Depends(require_admin)])
def update_item(item_id: int, item: ItemDetails):
    return call(service.save_item, item.model_dump(), item_id)


@router.delete('/watchlist/{item_id}', dependencies=[Depends(require_admin)])
def remove_item(item_id: int):
    call(service.delete_item, item_id)
    return ok({'deleted': True})


@router.post('/watchlist/quotes')
def quotes(request: QuoteRequest):
    return call(service.quote_batch, request.market, request.codes)
