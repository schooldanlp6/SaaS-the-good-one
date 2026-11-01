"""Tiny urllib helpers with timeouts and one error type."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

USER_AGENT = "SaaS"


class NetError(Exception):
    pass


def request(url: str, headers: Optional[Dict[str, str]] = None, data: Optional[bytes] = None,
            timeout: float = 8.0) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise NetError(f"HTTP {exc.code} from {_short(url)}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise NetError(f"{_short(url)}: {reason}") from None


def get_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: float = 8.0) -> Any:
    raw = request(url, headers, timeout=timeout)
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise NetError(f"{_short(url)}: not JSON") from None


def query(params: Dict[str, Any]) -> str:
    return urllib.parse.urlencode(params, doseq=True)


def _short(url: str) -> str:
    """URL without the query string - never log credentials or tokens."""
    url = url.split("?", 1)[0]
    scheme, sep, rest = url.partition("://")
    if sep and "@" in rest.split("/", 1)[0]:
        rest = "***@" + rest.split("@", 1)[1]
    return scheme + sep + rest
