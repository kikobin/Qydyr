"""Polite HTTP fetcher: robots.txt, per-host delay, retries, on-disk cache.

Every page we parse is kept in data/cache, so an extraction can always be traced back
to the exact HTML it came from.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import settings

log = logging.getLogger(__name__)


class FetchError(RuntimeError):
    pass


class RobotsDisallowed(FetchError):
    pass


@dataclass
class Page:
    url: str
    status: int
    text: str
    fetched_at: datetime
    from_cache: bool = False


class PoliteFetcher:
    def __init__(
        self,
        user_agent: str = settings.USER_AGENT,
        delay_s: float = settings.FETCH_DELAY_S,
        cache_dir: Path | None = None,
        cache_ttl_h: float = settings.CACHE_TTL_H,
        timeout_s: float = 25.0,
        respect_robots: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.delay_s = delay_s
        self.cache_dir = cache_dir or (settings.DATA_DIR / "cache" / "http")
        self.cache_ttl_s = cache_ttl_h * 3600
        self.respect_robots = respect_robots
        self.client = client or httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "ru,kk;q=0.9,en;q=0.8"},
            timeout=timeout_s,
            follow_redirects=True,
        )
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self.host_delays: dict[str, float] = {}   # per-host override (a source's delay_s, robots Crawl-delay)
        self._blocked: set[str] = set()           # hosts that sent us to a waiting room: leave them alone

    # ---------- robots.txt ----------
    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.client.get(host + "/robots.txt")
                if r.status_code >= 400:
                    rp = None  # no robots.txt: everything allowed
                else:
                    rp.parse(r.text.splitlines())
            except httpx.HTTPError as e:
                log.warning("robots.txt unavailable for %s (%s); proceeding carefully", host, e)
                rp = None
            self._robots[host] = rp
            delay = rp.crawl_delay(self.user_agent) if rp else None
            if delay:
                self.host_delays[parts.netloc] = max(float(delay), self.host_delays.get(parts.netloc, 0.0))
        rp = self._robots[host]
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    # ---------- cache ----------
    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        h = hashlib.sha1(url.encode()).hexdigest()
        return self.cache_dir / f"{h}.html", self.cache_dir / f"{h}.json"

    def _from_cache(self, url: str) -> Page | None:
        body, meta = self._cache_paths(url)
        if not (body.exists() and meta.exists()):
            return None
        m = json.loads(meta.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(m["fetched_at"])
        if (datetime.now(timezone.utc) - fetched).total_seconds() > self.cache_ttl_s:
            return None
        return Page(url=m["url"], status=m["status"], text=body.read_text(encoding="utf-8"), fetched_at=fetched, from_cache=True)

    def _to_cache(self, page: Page) -> None:
        body, meta = self._cache_paths(page.url)
        body.parent.mkdir(parents=True, exist_ok=True)
        body.write_text(page.text, encoding="utf-8")
        meta.write_text(json.dumps({"url": page.url, "status": page.status, "fetched_at": page.fetched_at.isoformat()}), encoding="utf-8")

    # ---------- fetch ----------
    def _wait_turn(self, url: str) -> None:
        host = urlsplit(url).netloc
        delay = max(self.delay_s, self.host_delays.get(host, 0.0))
        wait = delay - (time.monotonic() - self._last_hit.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def get(self, url: str, *, use_cache: bool = True) -> Page:
        if use_cache and (cached := self._from_cache(url)):
            return cached
        if not self.allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        host = urlsplit(url).netloc
        if host in self._blocked:
            raise FetchError(f"{host} is rate-limiting us; skipped {url}")
        last_err: Exception | None = None
        for attempt in range(3):
            self._wait_turn(url)
            try:
                r = self.client.get(url)
                if r.status_code >= 500:
                    raise FetchError(f"HTTP {r.status_code}")
                if r.status_code >= 400:
                    raise FetchError(f"HTTP {r.status_code} for {url}")
                final = r.url.host or ""
                if final != host and final.startswith("queue."):  # Queue-it waiting room = rate limit
                    self._blocked.add(host)
                    log.warning("%s sent us to a waiting room (%s); stopping requests to it", host, final)
                    raise FetchError(f"HTTP 429 (waiting room) for {url}")
                page = Page(url=str(r.url), status=r.status_code, text=r.text, fetched_at=datetime.now(timezone.utc))
                self._to_cache(page)
                return page
            except (httpx.HTTPError, FetchError) as e:
                last_err = e
                if isinstance(e, FetchError) and "HTTP 4" in str(e):
                    break
                time.sleep(2 * (attempt + 1))
        raise FetchError(f"failed to fetch {url}: {last_err}")
