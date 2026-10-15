"""Read APIs plus fail-closed admin-authenticated watchlist writes."""
import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field, field_validator

from web import ok
from web.services import watchlist_service as service
from web.services.research_reader import ResearchReadError

router = APIRouter()
logger = logging.getLogger(__name__)
Market = Literal['US', 'CN_HK', 'CN_A']

SESSION_COOKIE = 'watchlist_admin'
SESSION_MAX_AGE = 30 * 86400


def _admin_token() -> str:
    return os.environ.get('STOCK_WATCHLIST_ADMIN_TOKEN', '')


def _session_value(token: str) -> str:
    ts = str(int(time.time()))
    sig = hmac.new(token.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return f'{ts}.{sig}'


def _session_valid(value: str | None, token: str) -> bool:
    if not value or '.' not in value:
        return False
    ts, sig = value.rsplit('.', 1)
    try:
        age = int(time.time()) - int(ts)
    except ValueError:
        return False
    if age < 0 or age > SESSION_MAX_AGE:
        return False
    expect = hmac.new(token.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expect)


def require_admin(authorization: str | None = Header(default=None),
                  watchlist_admin: str | None = Cookie(default=None)):
    token = _admin_token()
    if len(token) < 24:
        raise HTTPException(503, '请配置至少24字符的 STOCK_WATCHLIST_ADMIN_TOKEN 后启用关注列表编辑')
    supplied = authorization.removeprefix('Bearer ') if authorization else ''
    if authorization and authorization.startswith('Bearer ') \
            and secrets.compare_digest(supplied.encode(), token.encode()):
        return
    if _session_valid(watchlist_admin, token):
        return
    raise HTTPException(401, '未登录或凭据无效')


class LoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)


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


class SuggestRequest(BaseModel):
    market: Market
    stock_code: str = Field(min_length=1, max_length=20, pattern=r'^[A-Za-z0-9.\-]+$')
    stock_name: str = Field(min_length=1, max_length=200)


def call(operation, *args):
    try:
        return ok(operation(*args))
    except ResearchReadError as exc:
        logger.warning('research read failed: %s', exc)
        raise HTTPException(503, f'研究读取失败：{exc}') from exc
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


@router.post('/watchlist/suggest', dependencies=[Depends(require_admin)])
def suggest(request: SuggestRequest):
    return call(service.suggest_fields, request.market, request.stock_code, request.stock_name)


@router.post('/watchlist/login')
def login(payload: LoginRequest, response: Response):
    token = _admin_token()
    if len(token) < 24:
        raise HTTPException(503, '请配置至少24字符的 STOCK_WATCHLIST_ADMIN_TOKEN 后启用关注列表编辑')
    if not secrets.compare_digest(payload.token.encode(), token.encode()):
        raise HTTPException(401, '凭据无效')
    response.set_cookie(SESSION_COOKIE, _session_value(token), max_age=SESSION_MAX_AGE,
                        httponly=True, samesite='lax', path='/')
    return ok({'authenticated': True})


@router.post('/watchlist/logout')
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path='/')
    return ok({'authenticated': False})


@router.get('/watchlist/session')
def session(watchlist_admin: str | None = Cookie(default=None)):
    token = _admin_token()
    authenticated = _session_valid(watchlist_admin, token) if len(token) >= 24 else False
    return ok({'authenticated': authenticated})
