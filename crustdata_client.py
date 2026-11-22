"""Crustdata API client with a SQLite response cache, a /web rate limiter,
retry-with-backoff, a credit limit and a cost ledger.

Every request goes through ``CrustdataClient.call``:
  1. The method, path and canonical JSON body are hashed (SHA-256).
  2. The SQLite cache is checked; a hit that is still fresh (``ttl_hours``)
     returns the stored response with no network call and no rate-limit cost.
     It keeps the date it was first retrieved, so facts are never re-dated.
  3. With ``offline=True`` a miss raises NotInCache instead of calling the API.
  4. On a miss, the run's credit limit is checked, the call waits on its
     endpoint's per-minute rate limit, the API is called, 429s, 5xx errors and
     network failures are retried with backoff, and 2xx responses are cached.
  5. Every call, cached or not, is written to the cost ledger.

A source that keeps failing raises SourceUnavailable: the caller must hold the
case for a person, never read the silence as "no results".

``request`` returns just the parsed JSON, exactly as the API sent it. ``call``
also returns when it was retrieved, whether it came from the cache, and what
it cost. This module does not read any response fields.

Endpoints and headers follow the Crustdata docs, API version 2025-11-01
(https://docs.crustdata.com).

The API key is read from the CRUSTDATA_API_KEY environment variable.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ledger

BASE_URL = "https://api.crustdata.com"
API_VERSION = "2025-11-01"
API_KEY_ENV = "CRUSTDATA_API_KEY"
DEFAULT_CACHE_PATH = Path(__file__).resolve().parent / ".crustdata_cache.sqlite3"

# Requests per minute our plan allows, by path prefix (account endpoints page, 22 Nov 2026).
# Paths not listed have no limit.
RATE_LIMITS_PER_MINUTE = {
    "/company/identify": 5,
    "/person/identify": 5,
    "/web/": 10,
    "/company/enrich": 15,
    "/person/enrich": 15,
    "/company/search": 30,
    "/person/search": 30,
    "/job/search": 30,
}

MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0

# Worth retrying: rate limited, server errors. status 0 = no response at all.
RETRYABLE_STATUSES = {0, 429, 500, 502, 503, 504}


class CrustdataError(Exception):
    """Raised for non-2xx responses that retrying won't fix (bad request, no access, no credits)."""

    def __init__(self, status: int, body: Any, path: str):
        self.status = status
        self.body = body
        self.path = path
        super().__init__(f"Crustdata {path} returned HTTP {status}: {body!r}")


class SourceUnavailable(CrustdataError):
    """The source kept failing after every retry. Hold the case for a person."""


class NotInCache(Exception):
    """Offline mode is on and this request was never cached."""


class BudgetExceeded(Exception):
    """This run has spent its credit limit."""


@dataclass
class ApiResponse:
    data: Any
    retrieved_at: str      # when Crustdata returned it (ISO, UTC); kept when served from the cache
    from_cache: bool
    credits: float | None  # charged for this call; 0 from the cache; None if the API didn't say


class RateLimiter:
    """Sliding window: at most ``max_requests`` in any 60 seconds."""

    def __init__(self, max_requests: int):
        self.max_requests = max_requests
        self._calls: deque[float] = deque()

    def acquire(self) -> None:
        while True:
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= 60:
                self._calls.popleft()
            if len(self._calls) < self.max_requests:
                self._calls.append(now)
                return
            time.sleep(60 - (now - self._calls[0]))


class ResponseCache:
    """SQLite store of responses keyed by request hash."""

    def __init__(self, path: str | os.PathLike[str]):
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS responses (
                request_hash TEXT PRIMARY KEY,
                method       TEXT NOT NULL,
                path         TEXT NOT NULL,
                request_body TEXT NOT NULL,
                status       INTEGER NOT NULL,
                response     TEXT NOT NULL,
                created_at   REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    def get(self, request_hash: str) -> tuple[Any, float] | None:
        """Return (response, created_at unix seconds), or None if not cached."""
        row = self._conn.execute(
            "SELECT response, created_at FROM responses WHERE request_hash = ?",
            (request_hash,),
        ).fetchone()
        return None if row is None else (json.loads(row[0]), row[1])

    def put(
        self,
        request_hash: str,
        method: str,
        path: str,
        body_text: str,
        status: int,
        response: Any,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO responses VALUES (?, ?, ?, ?, ?, ?, ?)",
            (request_hash, method, path, body_text, status, json.dumps(response), time.time()),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def canonical_body(body: Any) -> str:
    """Stable JSON text for hashing: sorted keys, no extra whitespace."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_hash(method: str, path: str, body: Any) -> str:
    # The path is part of the key so identical bodies sent to different
    # endpoints do not share a cache entry.
    payload = f"{method.upper()} {path}\n{canonical_body(body)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CrustdataClient:
    def __init__(
        self,
        api_key: str | None = None,
        cache_path: str | os.PathLike[str] = DEFAULT_CACHE_PATH,
        timeout: float = 120.0,
        ttl_hours: dict[str, float] | None = None,
        offline: bool = False,
        run_id: str = "adhoc",
        ledger_db: sqlite3.Connection | None = None,
        credit_limit: float | None = None,
    ):
        """
        ttl_hours:    max cache age per path, plus a "default" key; None = never expires.
        offline:      only serve from the cache (offline demo backup).
        run_id:       which run the ledger charges this client's calls to.
        ledger_db:    connection from agent_db.connect(); None = no ledger.
        credit_limit: stop new API calls once this run has spent this many credits.
        """
        self.api_key = api_key or os.environ.get(API_KEY_ENV)
        if not self.api_key and not offline:
            raise RuntimeError(f"Set the {API_KEY_ENV} environment variable.")
        self.timeout = timeout
        self.cache = ResponseCache(cache_path)
        self.limiters = {prefix: RateLimiter(n) for prefix, n in RATE_LIMITS_PER_MINUTE.items()}
        self.ttl_hours = ttl_hours
        self.offline = offline
        self.run_id = run_id
        self.ledger_db = ledger_db
        self.credit_limit = credit_limit

    # -- core ---------------------------------------------------------------

    def request(self, path: str, body: Any, method: str = "POST") -> Any:
        """Cached, rate-limited, retrying call. Returns the parsed JSON response."""
        return self.call(path, body, method).data

    def call(self, path: str, body: Any, method: str = "POST") -> ApiResponse:
        """Like ``request``, but also says when the data was retrieved and what it cost."""
        key = request_hash(method, path, body)

        cached = self.cache.get(key)
        if cached is not None and self._is_fresh(path, cached[1]):
            response, created_at = cached
            self._record(path, "cached", key, credits=0)
            return ApiResponse(response, _iso(created_at), from_cache=True, credits=0)

        if self.offline:
            raise NotInCache(f"{path} was never cached (request {key[:12]})")
        self._check_credit_limit()

        started = time.monotonic()
        try:
            status, headers, response = self._send_with_retry(method, path, body)
        except CrustdataError:
            self._record(path, "error", key)
            raise
        retrieved = time.time()
        self.cache.put(key, method, path, canonical_body(body), status, response)

        credits = _credits_used(headers)
        duration_ms = int((time.monotonic() - started) * 1000)
        self._record(path, "ok", key, credits=credits, duration_ms=duration_ms)
        return ApiResponse(response, _iso(retrieved), from_cache=False, credits=credits)

    def _is_fresh(self, path: str, created_at: float) -> bool:
        if self.offline or self.ttl_hours is None:
            return True
        max_age_hours = self.ttl_hours.get(path, self.ttl_hours["default"])
        return time.time() - created_at <= max_age_hours * 3600

    def _check_credit_limit(self) -> None:
        if self.credit_limit is None or self.ledger_db is None:
            return
        spent = ledger.credits_spent(self.ledger_db, self.run_id)
        if spent >= self.credit_limit:
            raise BudgetExceeded(f"Run {self.run_id} has spent {spent} of {self.credit_limit} credits")

    def _record(self, path: str, status: str, key: str, credits: float | None = None,
                duration_ms: int | None = None) -> None:
        if self.ledger_db is not None:
            ledger.record(self.ledger_db, self.run_id, "crustdata", path, status,
                          credits=credits, request_hash=key, duration_ms=duration_ms)

    def _send_with_retry(self, method: str, path: str, body: Any) -> tuple[int, dict[str, str], Any]:
        limiter = self._limiter_for(path)
        for attempt in range(MAX_RETRIES + 1):
            if limiter:
                limiter.acquire()
            try:
                status, headers, response = self._send(method, path, body)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
                # No response at all: timeout, DNS failure, dropped connection.
                status, headers, response = 0, {}, f"{type(err).__name__}: {err}"

            if 200 <= status < 300:
                return status, headers, response
            if status not in RETRYABLE_STATUSES:
                raise CrustdataError(status, response, path)
            if attempt == MAX_RETRIES:
                raise SourceUnavailable(status, response, path)
            time.sleep(self._backoff_seconds(attempt, headers))
        raise AssertionError("unreachable")

    def _limiter_for(self, path: str) -> RateLimiter | None:
        for prefix, limiter in self.limiters.items():
            if path.startswith(prefix):
                return limiter
        return None

    @staticmethod
    def _backoff_seconds(attempt: int, headers: dict[str, str]) -> float:
        """Wait as long as x-ratelimit-reset says (docs), otherwise 2s, 4s, 8s, ... up to 60s."""
        try:
            return min(float(headers["x-ratelimit-reset"]), BACKOFF_MAX_SECONDS)
        except (KeyError, ValueError):
            return min(BACKOFF_BASE_SECONDS * (2 ** attempt), BACKOFF_MAX_SECONDS)

    def _send(self, method: str, path: str, body: Any) -> tuple[int, dict[str, str], Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            BASE_URL + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "x-api-version": API_VERSION,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, _lower_headers(resp.headers), _parse(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, _lower_headers(err.headers), _parse(err.read())

    # -- endpoints (bodies are passed through as the docs define them) -------

    def company_identify(self, body: dict[str, Any]) -> Any:
        return self.request("/company/identify", body)

    def company_search(self, body: dict[str, Any]) -> Any:
        return self.request("/company/search", body)

    def person_search(self, body: dict[str, Any]) -> Any:
        return self.request("/person/search", body)

    def job_search(self, body: dict[str, Any]) -> Any:
        return self.request("/job/search", body)

    def social_post_search(self, body: dict[str, Any]) -> Any:
        return self.request("/social_post/search", body)

    def web_search(self, body: dict[str, Any]) -> Any:
        return self.request("/web/search/live", body)

    def web_fetch(self, body: dict[str, Any]) -> Any:
        return self.request("/web/enrich/live", body)

    def close(self) -> None:
        self.cache.close()


def _credits_used(headers: dict[str, str]) -> float | None:
    value = headers.get("x-credits-used")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _iso(unix_seconds: float) -> str:
    return datetime.fromtimestamp(unix_seconds, timezone.utc).isoformat(timespec="seconds")


def _lower_headers(headers: Any) -> dict[str, str]:
    return {k.lower(): v for k, v in (headers.items() if headers else [])}


def _parse(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw_text": text}
