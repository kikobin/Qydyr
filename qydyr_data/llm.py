"""Claude API — the single place for every LLM call in the project.

- structured(): JSON that matches a Pydantic schema (structured outputs, `messages.parse`)
- text(): plain text (translations)
Results of structured calls are cached on disk by content hash: re-running the pipeline on
unchanged pages costs nothing. Models: Haiku 5.5 for volume (extraction, understanding),
Sonnet 5.5 where quality of language matters (Kazakh / English answers, photos).
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from . import settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class Claude:
    def __init__(self, model: str | None = None, *, api_key: str | None = None, client=None,
                 cache_dir: Path | None = None, fast: bool = False) -> None:
        if client is None:
            from anthropic import Anthropic

            key = api_key or settings.ANTHROPIC_API_KEY
            if not key:
                raise RuntimeError("ANTHROPIC_API_KEY is not set (or run without LLM: --no-llm)")
            client = Anthropic(api_key=key)
        self.client = client
        self.model = model or settings.CLAUDE_MODEL
        self.cache_dir = cache_dir
        self.fast = fast

    def _thinking(self) -> dict:
        """Short, latency-bound calls (a request to parse, three sentences to write, strings to translate)
        don't need thinking, and with it the person waits twice as long. Haiku 5.5 turns it off with
        "disabled"; Sonnet 5.5 rejects that and takes "between_tools"; other models keep their default."""
        if not self.fast:
            return {}
        if self.model.startswith("claude-haiku-5"):
            return {"thinking": {"type": "disabled"}}
        if self.model.startswith("claude-sonnet-5-5"):
            return {"thinking": {"type": "between_tools"}}
        return {}

    def _cache_path(self, *parts: str) -> Path | None:
        if self.cache_dir is None:
            return None
        h = hashlib.sha1("\x1f".join((self.model, *parts)).encode()).hexdigest()
        return self.cache_dir / f"{h}.json"

    def structured(self, *, system: str, user: str | list, schema: type[T], max_tokens: int = 8192) -> T:
        cache = self._cache_path(system, str(user), schema.__name__)
        if cache is not None and cache.exists():
            return schema.model_validate_json(cache.read_text(encoding="utf-8"))
        resp = self.client.messages.parse(
            model=self.model,
            max_tokens=max_tokens,
            # the long, repeated instructions are cached by the API between calls
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            **self._thinking(),
        )
        out = resp.parsed_output
        if out is None:
            raise RuntimeError(f"Claude returned no structured output (stop_reason={resp.stop_reason})")
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(out.model_dump_json(), encoding="utf-8")
        return out

    def text(self, *, system: str, user: str | list, max_tokens: int = 4096) -> str:
        resp = self.client.messages.create(
            model=self.model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}], **self._thinking(),
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
