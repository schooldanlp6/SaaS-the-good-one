"""`main.py --test-network`: try every [[network]] server from the config the
way the player would use it, and say plainly what works and what doesn't."""
from __future__ import annotations

import base64
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Tuple

from . import http


def probe_stream(url: str, timeout: float = 8.0) -> Tuple[bool, str]:
    """Fetch the first KB of a stream URL. user:password@ in the URL becomes Basic auth."""
    parts = urllib.parse.urlsplit(url)
    headers = {"User-Agent": http.USER_AGENT, "Range": "bytes=0-1023"}
    if parts.username is not None:
        cred = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password or '')}"
        headers["Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
        netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
        url = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            data = r.read(1024)
            ctype = r.headers.get("Content-Type", "?")
            return bool(data), f"HTTP {r.status}, {ctype}, {len(data)} bytes"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, str(getattr(exc, "reason", exc))


def run(specs: List[Dict], make_factory: Callable, out=print) -> int:
    if not specs:
        out("no [[network]] servers in the config")
        return 1
    failed = 0
    for spec in specs:
        out(f"== {spec['name']} ({spec['type']})")
        try:
            lib = make_factory(spec)()
            t0 = time.monotonic()
            folders = lib.scan()
            out(f"   connected, {len(folders)} folder(s) in {time.monotonic() - t0:.1f} s")
            if not folders:
                out("   FAIL: no music found")
                failed += 1
                continue
            idx = next((i for i in range(len(folders)) if lib.tracks(i)), None)
            if idx is None:
                out("   FAIL: the folders have no playable tracks")
                failed += 1
                continue
            out(f"   first folder with tracks: {lib.folder_label(idx)} ({len(lib.tracks(idx))} tracks)")
            tags = lib.track_tags(idx, 0) or {}
            out(f"   first track: {tags.get('title') or lib.track_label(idx, 0)!r}"
                f" by {tags.get('artist') or '?'!r}, {float(tags.get('duration') or 0):.0f} s")
            ok, detail = probe_stream(lib.track_path(idx, 0))
            out(f"   stream: {'OK' if ok else 'FAIL'} ({detail})")
            if ok:
                lib.ping()
                out("   OK")
            else:
                failed += 1
        except Exception as exc:  # report, then go on with the next server
            out(f"   FAIL: {exc}")
            failed += 1
    return 1 if failed else 0
