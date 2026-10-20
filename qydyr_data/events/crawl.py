"""Walk configured sources and turn pages / posts into RawEvents.

Source kinds (config/sources.yaml):
  listing        — afisha listing page -> event pages (link_pattern) -> one event per page
  article_index  — tag/rubric page -> digest articles ("куда сходить на выходных") -> many events each
  page           — a single page (a venue's "exhibitions" page), mode page or digest
  telegram       — public channels via t.me/s/<channel>
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .. import settings
from ..fetch import FetchError, PoliteFetcher
from . import jsonld, telegram
from .llm import Extractor, evidence_found
from .models import ExtractedEvent, RawEvent
from .pagetext import find_links, main_text
from .ru_dates import dates_consistent
from .sessions import VENUE_HOURS_KINDS, parse_date

log = logging.getLogger(__name__)


@dataclass
class SourceConfig:
    id: str
    kind: str
    url: str | None = None
    title: str | None = None
    link_pattern: str | None = None
    link_text_pattern: str | None = None
    mode: str = "page"
    max_items: int = 30
    pages: int = 1                 # listing: also read ?page=2..N
    delay_s: float | None = None   # seconds between requests to this source's host (default: QYDYR_FETCH_DELAY)
    reliability: float = 0.8
    venue_hint: str | None = None
    kind_hint: str | None = None
    is_free_hint: bool | None = None
    channels: list[str] = field(default_factory=list)
    max_age_days: int = 14
    enabled: bool = True


def load_sources(path: Path | None = None) -> list[SourceConfig]:
    p = path or settings.CONFIG_DIR / "sources.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return [SourceConfig(**s) for s in data.get("sources", [])]


@dataclass
class SourceStats:
    source_id: str
    pages: int = 0
    events: int = 0
    jsonld: int = 0
    llm: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.errors and not self.events:
            return "error: " + "; ".join(self.errors[:3])
        return f"ok: {self.pages} pages, {self.events} events" + (f", {len(self.errors)} errors" if self.errors else "")


class Collector:
    def __init__(self, fetcher: PoliteFetcher, extractor: Extractor | None, today: date | None = None) -> None:
        self.fetcher = fetcher
        self.extractor = extractor
        self.today = today or datetime.now(settings.TZ).date()

    def collect(self, sources: list[SourceConfig]) -> tuple[list[RawEvent], list[SourceStats]]:
        raws: list[RawEvent] = []
        stats: list[SourceStats] = []
        for src in sources:
            if not src.enabled:
                continue
            st = SourceStats(src.id)
            if src.delay_s and src.url and hasattr(self.fetcher, "host_delays"):
                host = urlsplit(src.url).netloc
                self.fetcher.host_delays[host] = max(src.delay_s, self.fetcher.host_delays.get(host, 0.0))
            try:
                handler = {
                    "listing": self._listing, "article_index": self._article_index,
                    "page": self._single_page, "telegram": self._telegram,
                }[src.kind]
                got = handler(src, st)
                raws += got
                st.events = len(got)
            except Exception as e:  # one broken source must not stop the others
                log.exception("source %s failed", src.id)
                st.errors.append(str(e))
            log.info("source %-28s %s", src.id, st.status)
            stats.append(st)
        return raws, stats

    # ----- source kinds -----
    def _listing(self, src: SourceConfig, st: SourceStats) -> list[RawEvent]:
        links: list[str] = []
        for n in range(1, max(src.pages, 1) + 1):
            url = src.url if n == 1 else f"{src.url}{'&' if '?' in src.url else '?'}page={n}"
            try:
                index = self.fetcher.get(url)
            except FetchError as e:
                if n == 1:
                    raise
                st.errors.append(str(e))
                break
            st.pages += 1
            new = [u for u in find_links(index.text, index.url, src.link_pattern, src.link_text_pattern)
                   if u not in links]
            if not new:  # past the last page
                break
            links += new
        links = links[: src.max_items]
        out: list[RawEvent] = []
        for url in links:
            out += self._event_page(url, src, st, mode="page")
        return out

    def _article_index(self, src: SourceConfig, st: SourceStats) -> list[RawEvent]:
        index = self.fetcher.get(src.url)
        st.pages += 1
        links = find_links(index.text, index.url, src.link_pattern, src.link_text_pattern)[: src.max_items]
        out: list[RawEvent] = []
        for url in links:
            out += self._event_page(url, src, st, mode="digest")
        return out

    def _single_page(self, src: SourceConfig, st: SourceStats) -> list[RawEvent]:
        return self._event_page(src.url, src, st, mode=src.mode)

    def _telegram(self, src: SourceConfig, st: SourceStats) -> list[RawEvent]:
        out: list[RawEvent] = []
        run_end = datetime.combine(self.today, time.max, tzinfo=settings.TZ)
        for ch in src.channels:
            page = self.fetcher.get(telegram.channel_url(ch))
            st.pages += 1
            posts = telegram.parse_channel_page(page.text)
            for post in telegram.fresh_event_posts(posts, src.max_age_days, now=run_end):
                if self.extractor is None:
                    continue
                ref = post.published_at.astimezone(settings.TZ).date()
                try:
                    found = self.extractor.extract(post.text, mode="post", ref_date=ref, url=post.url,
                                                   venue_hint=src.venue_hint)
                except Exception as e:
                    st.errors.append(f"{post.url}: {e}")
                    continue
                st.llm += 1
                for ev in found:
                    out.append(self._raw(ExtractedEvent(**ev.model_dump()), src, post.url, "telegram", "post",
                                         ref, post.published_at, post.text))
        return out

    # ----- one page -----
    def _event_page(self, url: str, src: SourceConfig, st: SourceStats, mode: str) -> list[RawEvent]:
        try:
            page = self.fetcher.get(url)
        except FetchError as e:
            st.errors.append(str(e))
            return []
        st.pages += 1

        if mode == "page":
            structured = jsonld.extract(page.text, page.url)
            if structured:
                st.jsonld += 1
                # on an event page the main event is the one pointing at this URL (or the first one)
                main = [e for e in structured if e.url and e.url.rstrip("/") == page.url.rstrip("/")] or structured[:1]
                if not any(self._repertory_span(e) for e in main):
                    return [self._raw(e, src, page.url, "jsonld", "jsonld", self.today, page.fetched_at, None)
                            for e in main]
                # the dates are "first show ever – last show": the real sessions are in the page text
                log.debug("repertory span in JSON-LD on %s; reading the sessions from the text", page.url)

        if self.extractor is None:
            return []
        pt = main_text(page.text)
        try:
            found = self.extractor.extract(pt.text, mode=mode, ref_date=self.today, title_hint=pt.title,
                                           venue_hint=src.venue_hint, url=page.url)
        except Exception as e:
            st.errors.append(f"{url}: LLM {e}")
            return []
        st.llm += 1
        out = []
        for ev in found:
            e = ExtractedEvent(**ev.model_dump())
            if mode == "page":
                e.url = e.url or page.url
                e.image_url = e.image_url or pt.og_image
            out.append(self._raw(e, src, page.url, "llm", mode, self.today, page.fetched_at, pt.text))
        return out

    def _repertory_span(self, e: ExtractedEvent) -> bool:
        """Ticket sites mark a repertory show with startDate = its first show ever and endDate = the last one
        on sale ("Волк и 7 козлят", 20.02–31.10, 18:00). Read as a period, it would invent a show every day.
        Only exhibitions and fairs really run every day of their range; anything else is checked against the text."""
        d1, d2 = parse_date(e.date_start), parse_date(e.date_end)
        return bool(d1 and d2 and d2 > d1 and self.today <= d2
                    and e.kind not in VENUE_HOURS_KINDS and not e.schedule_opening_hours)

    def _raw(self, e: ExtractedEvent, src: SourceConfig, url: str, extraction: str, mode: str,
             ref: date, fetched_at: datetime, source_text: str | None) -> RawEvent:
        ev_ok = True if extraction == "jsonld" else evidence_found(e.evidence, source_text or "")
        wanted = [parse_date(e.date_start), parse_date(e.date_end)] + [parse_date(s.date) for s in e.sessions]
        consistent = None if extraction == "jsonld" else dates_consistent([d for d in wanted if d], e.evidence, ref)
        return RawEvent(
            event=e, source_id=src.id, source_url=url, extraction=extraction, mode=mode, reference_date=ref,
            fetched_at=fetched_at if fetched_at.tzinfo else fetched_at.replace(tzinfo=timezone.utc),
            reliability=src.reliability, venue_hint=src.venue_hint, kind_hint=src.kind_hint,
            is_free_hint=src.is_free_hint, evidence_ok=ev_ok, dates_consistent=consistent,
        )
