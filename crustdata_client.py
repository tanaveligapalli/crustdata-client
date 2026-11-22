"""Crustdata API client with a SQLite response cache, a /web rate limiter,
and retry-with-backoff on HTTP 429.

Every request goes through ``CrustdataClient.request``:
  1. The method, path and canonical JSON body are hashed (SHA-256).
  2. The SQLite cache is checked; a hit returns the stored response with no
     network call and no rate-limit cost.
  3. On a miss, /web/* calls wait on a 10-requests-per-minute limiter, the API
     is called, 429s are retried with backoff, and 2xx responses are cached.

Responses are returned as parsed JSON exactly as the API sent them. This
module does not read any response fields.

Endpoints and headers follow the Crustdata docs, API version 2025-11-01
(https://docs.crustdata.com).

The API key is read from the CRUSTDATA_API_KEY environment variable.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any

BASE_URL = "https://api.crustdata.com"
API_VERSION = "2025-11-01"
API_KEY_ENV = "CRUSTDATA_API_KEY"
DEFAULT_CACHE_PATH = Path(__file__).resolve().parent / ".crustdata_cache.sqlite3"

WEB_PATH_PREFIX = "/web/"
WEB_MAX_REQUESTS = 10
WEB_WINDOW_SECONDS = 60.0

MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0


class CrustdataError(Exception):
    """Raised for non-2xx responses (after retries, for 429)."""

    def __init__(self, status: int, body: Any, path: str):
        self.status = status
        self.body = body
        self.path = path
        super().__init__(f"Crustdata {path} returned HTTP {status}: {body!r}")


class RateLimiter:
    """Sliding-window limiter: at most ``max_requests`` per ``window`` seconds."""

    def __init__(self, max_requests: int, window: float):
        self.max_requests = max_requests
        self.window = window
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.window:
                    self._calls.popleft()
                if len(self._calls) < self.max_requests:
                    self._calls.append(now)
                    return
                wait = self.window - (now - self._calls[0])
            time.sleep(max(wait, 0.01))


class ResponseCache:
    """SQLite store of responses keyed by request hash."""

    def __init__(self, path: str | os.PathLike[str]):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
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

    def get(self, request_hash: str) -> Any | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT response FROM responses WHERE request_hash = ?",
                (request_hash,),
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def put(
        self,
        request_hash: str,
        method: str,
        path: str,
        body_text: str,
        status: int,
        response: Any,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO responses VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    request_hash,
                    method,
                    path,
                    body_text,
                    status,
                    json.dumps(response),
                    time.time(),
                ),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
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
        base_url: str = BASE_URL,
        timeout: float = 120.0,
    ):
        self.api_key = api_key or os.environ.get(API_KEY_ENV)
        if not self.api_key:
            raise RuntimeError(f"Set the {API_KEY_ENV} environment variable.")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cache = ResponseCache(cache_path)
        self.web_limiter = RateLimiter(WEB_MAX_REQUESTS, WEB_WINDOW_SECONDS)

    # -- core ---------------------------------------------------------------

    def request(self, path: str, body: Any, method: str = "POST") -> Any:
        """Cached, rate-limited, retrying call. Returns the parsed JSON response."""
        key = request_hash(method, path, body)
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        status, response = self._send_with_retry(method, path, body)
        self.cache.put(key, method, path, canonical_body(body), status, response)
        return response

    def _send_with_retry(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        for attempt in range(MAX_RETRIES + 1):
            if path.startswith(WEB_PATH_PREFIX):
                self.web_limiter.acquire()
            status, headers, response = self._send(method, path, body)
            if status != 429:
                if 200 <= status < 300:
                    return status, response
                raise CrustdataError(status, response, path)
            if attempt == MAX_RETRIES:
                raise CrustdataError(status, response, path)
            time.sleep(self._backoff_seconds(attempt, headers))
        raise AssertionError("unreachable")

    @staticmethod
    def _backoff_seconds(attempt: int, headers: dict[str, str]) -> float:
        # Docs: wait x-ratelimit-reset seconds, then back off exponentially
        # with jitter. Retry-After is honoured too if it ever appears.
        exp = min(BACKOFF_BASE_SECONDS * (2 ** attempt), BACKOFF_MAX_SECONDS)
        jitter = random.uniform(0, exp / 2)
        for name in ("x-ratelimit-reset", "retry-after"):
            value = headers.get(name)
            if value:
                try:
                    return float(value) + jitter
                except ValueError:
                    pass
        return exp + jitter

    def _send(self, method: str, path: str, body: Any) -> tuple[int, dict[str, str], Any]:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
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

    def job_search(self, body: dict[str, Any]) -> Any:
        return self.request("/job/search", body)

    def web_search(self, body: dict[str, Any]) -> Any:
        return self.request("/web/search/live", body)

    def web_fetch(self, body: dict[str, Any]) -> Any:
        return self.request("/web/enrich/live", body)

    def close(self) -> None:
        self.cache.close()


def _lower_headers(headers: Any) -> dict[str, str]:
    return {k.lower(): v for k, v in (headers.items() if headers else [])}


def _parse(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw_text": text}
