"""Telegram Mini App initData validation (HMAC-SHA256), so the API knows a request really comes
from the Mini App opened in Telegram. Algorithm (core.telegram.org/bots/webapps):
secret = HMAC_SHA256(key="WebAppData", msg=bot_token); check = HMAC_SHA256(key=secret, msg=data_check_string),
data_check_string = sorted "key=value" pairs except `hash`, joined with "\\n"."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def validate_init_data(init_data: str, bot_token: str, max_age_s: int = 24 * 3600,
                       now: float | None = None) -> dict | None:
    """The parsed fields (with `user` decoded) if the signature is valid and fresh, else None."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if max_age_s and (now or time.time()) - auth_date > max_age_s:
        return None
    if "user" in pairs:
        try:
            pairs["user"] = json.loads(pairs["user"])
        except ValueError:
            return None
    return pairs


def bot_key(bot_token: str) -> str:
    """What the bot sends to the API when they run as separate services: proves it knows the token, never reveals it."""
    return hmac.new(b"QydyrBot", bot_token.encode(), hashlib.sha256).hexdigest()


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """Build a valid initData string (tests and local demos)."""
    from urllib.parse import urlencode
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})
