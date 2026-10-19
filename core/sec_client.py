"""Shared SEC GET client: one database gate for every process and endpoint.

Cache hits never call this module's IO functions. No module import connects to
PostgreSQL. Coordination errors deliberately are not requests.RequestException,
so legacy optional-download handlers cannot mistake them for missing evidence.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import timezone
from email.utils import parsedate_to_datetime
import json
import logging
import math
from pathlib import Path
import subprocess
import sys
import os
import threading
import time
from urllib.parse import urljoin, urlsplit

import psycopg2
import requests

import config

logger = logging.getLogger(__name__)
GATE_KEY = "sec_edgar"
LOCK_KEY = 0x53454347415445
_process_lock = threading.Lock()


# A forked caller must not inherit a locked Python mutex.
def _after_fork() -> None:
    global _process_lock
    _process_lock = threading.Lock()

if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


class SECError(RuntimeError):
    """Explicit SEC request failure; must not be converted to an empty result."""


class SECCoordinationError(SECError):
    pass


class SECWaitTimeout(SECError):
    pass


class SECTransportError(SECError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class SECHTTPError(SECError):
    def __init__(self, response: requests.Response):
        self.response = response
        super().__init__(f"SEC HTTP {response.status_code}: {response.url}")


def _validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and
                 parsed.hostname in ("www.sec.gov", "data.sec.gov") and
                 parsed.port in (None, 443) and not parsed.username and
                 not parsed.password and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise SECError("SEC client only accepts HTTPS www.sec.gov/data.sec.gov URLs")


def _settings() -> tuple[float, dict[str, str]]:
    rate = config.sec.rate_limit
    if type(rate) is not int or rate not in (1, 2):
        raise SECError("STOCK_SEC_RATE_LIMIT must be 1 or 2; SEC network access blocked")
    ua = config.sec.user_agent
    if not isinstance(ua, str) or not ua.strip() or "\n" in ua or "\r" in ua:
        raise SECError("STOCK_SEC_USER_AGENT must be a nonempty single line")
    return 1.0 / rate, {"User-Agent": ua, "Accept": "*/*"}


def _connect():
    # Dedicated session, not the business pool: session locks must never leak
    # into pooled transactions. Short DB timeouts also bound failure detection.
    return psycopg2.connect(
        host=config.db.host, port=config.db.port, dbname=config.db.dbname,
        user=config.db.user, password=config.db.password, connect_timeout=2,
        options="-c statement_timeout=1000", keepalives=1,
        keepalives_idle=1, keepalives_interval=1, keepalives_count=2,
        tcp_user_timeout=2000, application_name="sec_request_gate",
    )


def _sleep_until_retry(seconds: float, deadline: float, reason: str) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0 or seconds >= remaining:
        raise SECWaitTimeout(f"{reason}; retry in at least {max(seconds, 0):.3f}s")
    time.sleep(max(0, seconds))


class _Gate:
    def __init__(self, conn, interval: float):
        self.conn = conn
        self.interval = interval

    def check(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT 1")

    def reserve(self, duration: float) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE public.sec_request_gate SET
                   next_allowed_at=clock_timestamp() + %s * interval '1 second',
                   updated_at=clock_timestamp() WHERE gate_key=%s""",
                (duration + self.interval + 2, GATE_KEY),
            )
            if cur.rowcount != 1:
                raise SECCoordinationError("SEC gate row disappeared")

    def finish(self, cooldown: float) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE public.sec_request_gate SET
                   next_allowed_at=clock_timestamp() + %s * interval '1 second',
                   blocked_until=GREATEST(blocked_until,
                       clock_timestamp() + %s * interval '1 second'),
                   updated_at=clock_timestamp() WHERE gate_key=%s""",
                (self.interval, cooldown, GATE_KEY),
            )
            if cur.rowcount != 1:
                raise SECCoordinationError("SEC gate state update failed")


@contextmanager
def _acquire_gate(deadline: float, interval: float):
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _process_lock.acquire(timeout=remaining):
        raise SECWaitTimeout("SEC in-process queue deadline exceeded")
    conn = None
    try:
        conn = _connect()
        conn.autocommit = True
        while True:
            if time.monotonic() >= deadline:
                raise SECWaitTimeout("SEC lock wait deadline exceeded")
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))
                locked = cur.fetchone()[0]
                if locked:
                    cur.execute(
                        """SELECT EXTRACT(EPOCH FROM
                           GREATEST(next_allowed_at, blocked_until)-clock_timestamp())
                           FROM public.sec_request_gate WHERE gate_key=%s""", (GATE_KEY,),
                    )
                    row = cur.fetchone()
                    if row is None or not math.isfinite(float(row[0])):
                        raise SECCoordinationError("SEC gate row missing/invalid; apply sec_request_gate.sql")
                    wait = max(0, float(row[0]))
                    if wait == 0:
                        break
                    cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
                else:
                    wait = 0.05
            _sleep_until_retry(wait, deadline, "SEC shared gate/cooldown wait")
        yield _Gate(conn, interval)
    except psycopg2.Error as exc:
        raise SECCoordinationError(
            "SEC coordination unavailable; check DB and scripts/sec_request_gate.sql"
        ) from exc
    finally:
        # Closing the dedicated session releases the lock even after an SQL error.
        if conn is not None:
            conn.close()
        _process_lock.release()


def _physical_get(url: str, headers: dict, deadline: float, check) -> requests.Response:
    """A killable child enforces wall time, including DNS and trickle downloads."""
    payload = json.dumps({"url": url, "headers": headers, "deadline": deadline,
                          "parent_pid": os.getpid()}).encode()
    worker = Path(__file__).with_name("sec_http_worker.py")
    proc = subprocess.Popen([sys.executable, str(worker)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SECTransportError("SEC HTTP total deadline exceeded")
            try:
                out, _err = proc.communicate(input=payload, timeout=min(0.5, remaining))
                break
            except subprocess.TimeoutExpired:
                payload = None
                check()  # A dead coordinator must not continue downloading/retrying.
        if proc.returncode != 0:
            raise SECTransportError(f"SEC HTTP worker ended with status {proc.returncode}")
        try:
            data = json.loads(out)
        except (ValueError, UnicodeError) as exc:
            raise SECTransportError("SEC HTTP worker returned invalid data", retryable=False) from exc
        if "error" in data:
            raise SECTransportError(data["error"], retryable=data["retryable"])
        response = requests.Response()
        response.status_code = data["status"]
        response.headers.update(data["headers"])
        response._content = base64.b64decode(data["body"])
        response.encoding = data["encoding"]
        response.url = data["url"]
        return response
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate()  # Reap/close pipes before the gate can be released.


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        if value.strip().isdigit():
            result = float(value.strip())
        else:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            result = max(0, dt.timestamp() - time.time())
        if math.isfinite(result) and result >= 0:
            return result
    except (ValueError, TypeError, OverflowError):
        pass
    logger.warning("SEC invalid Retry-After header")
    return None


def get(url: str, *, task: str, identity: str = "", timeout: float = 60,
        total_timeout: float = 180, allowed_statuses: tuple[int, ...] = ()) -> requests.Response:
    """GET with global serialization. Only explicitly allowed 404 is nonfatal."""
    _validate_url(url)
    interval, headers = _settings()
    if (not math.isfinite(timeout) or not 0 < timeout <= 60 or
            not math.isfinite(total_timeout) or not 0 < total_timeout <= 180):
        raise SECError("SEC budgets must be positive; HTTP <=60s, logical GET <=180s")
    if any(status != 404 for status in allowed_statuses):
        raise SECError("Only an explicit business 404 may bypass HTTP failure")
    deadline = time.monotonic() + total_timeout
    retries = 0
    redirects = 0
    started = time.monotonic()
    try:
        while True:
            response = None
            transport_error = None
            cooldown = 0.0
            with _acquire_gate(deadline, interval) as gate:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SECWaitTimeout("SEC deadline exceeded before send")
                http_deadline = time.monotonic() + min(timeout, remaining)
                gate.reserve(min(timeout, remaining))
                gate.check()
                try:
                    response = _physical_get(url, headers, http_deadline, gate.check)
                except SECTransportError as exc:
                    transport_error = exc
                    if exc.retryable:
                        cooldown = float(2 ** (retries + 1))
                if response is not None:
                    status = response.status_code
                    if status == 403:
                        cooldown = 60
                    elif status in (429, 500, 502, 503, 504):
                        cooldown = float(2 ** (retries + 1))
                        if status in (429, 503):
                            retry_after = _retry_after(response.headers.get("Retry-After"))
                            cooldown = max(cooldown, retry_after if retry_after is not None
                                           else (10 if status == 429 else 0))
                gate.finish(cooldown)
            logger.info("SEC task=%s identity=%s attempt=%s status=%s elapsed=%.3f cooldown=%.3f",
                        task, identity, retries + 1,
                        response.status_code if response is not None else "transport_error",
                        time.monotonic() - started, cooldown)
            if transport_error is not None:
                if not transport_error.retryable or retries >= 2:
                    raise transport_error
            elif response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location or redirects >= 3:
                    raise SECError("SEC invalid/excessive redirects")
                url = urljoin(url, location)
                _validate_url(url)
                redirects += 1
                continue
            elif response.status_code in (429, 500, 502, 503, 504):
                if retries >= 2:
                    raise SECHTTPError(response)
            elif 200 <= response.status_code < 300 or response.status_code in allowed_statuses:
                return response
            else:
                raise SECHTTPError(response)
            retries += 1
            # No local sleep: the persisted cooldown is rechecked under the
            # shared lock by this caller AND every other process.
    except SECError:
        logger.exception("SEC request failed task=%s identity=%s elapsed=%.3f",
                         task, identity, time.monotonic() - started)
        raise
