"""DLNA / UPnP media servers (minidlna, Jellyfin's DLNA plugin, Kodi, NAS boxes, ...).

Finding the server: `location` (its description URL, e.g.
http://192.168.1.5:8200/rootDesc.xml) if set, else SSDP discovery on the LAN,
optionally picking the server whose name contains `server`.

Folders: every container (below `start`, e.g. "Music/Folders") that directly
holds audio items. DLNA servers show the same songs in several views (by
artist, by genre, ...), so pointing `start` at one view keeps the list short.
"""
from __future__ import annotations

import logging
import socket
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import deque
from typing import List, Tuple

from . import http
from .base import NetLibrary, Track

log = logging.getLogger(__name__)

SSDP_ADDR = ("239.255.255.250", 1900)
MEDIA_SERVER = "urn:schemas-upnp-org:device:MediaServer:1"
CONTENT_DIRECTORY = "urn:schemas-upnp-org:service:ContentDirectory:1"
NS = {
    "d": "urn:schemas-upnp-org:device-1-0",
    "didl": "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "upnp": "urn:schemas-upnp-org:metadata-1-0/upnp/",
}
MAX_CONTAINERS = 2000   # safety net for huge servers
PAGE = 200


def discover(timeout: float = 3.0) -> List[str]:
    """Description URLs of the media servers that answer an SSDP search."""
    msg = ("M-SEARCH * HTTP/1.1\r\n"
           f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
           'MAN: "ssdp:discover"\r\nMX: 2\r\n'
           f"ST: {MEDIA_SERVER}\r\n\r\n").encode()
    found: List[str] = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.5)
        s.sendto(msg, SSDP_ADDR)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, _ = s.recvfrom(4096)
            except socket.timeout:
                continue
            for line in data.decode("utf-8", "replace").split("\r\n"):
                key, _, value = line.partition(":")
                if key.strip().lower() == "location" and value.strip() not in found:
                    found.append(value.strip())
    except OSError as exc:
        log.warning("SSDP discovery failed: %s", exc)
    finally:
        s.close()
    return found


def describe(location: str) -> Tuple[str, str]:
    """(friendlyName, ContentDirectory control URL) from a device description."""
    try:
        root = ET.fromstring(http.request(location, timeout=5))
    except ET.ParseError:
        raise http.NetError(f"{location}: not a UPnP description") from None
    base = root.findtext("d:URLBase", default="", namespaces=NS).strip() or location
    for dev in root.iter(f"{{{NS['d']}}}device"):
        name = dev.findtext("d:friendlyName", default="", namespaces=NS).strip()
        for svc in dev.iter(f"{{{NS['d']}}}service"):
            if svc.findtext("d:serviceType", default="", namespaces=NS).startswith(CONTENT_DIRECTORY[:-2]):
                control = svc.findtext("d:controlURL", default="", namespaces=NS).strip()
                return name, urllib.parse.urljoin(base, control)
    raise http.NetError(f"{location}: no ContentDirectory service (not a media server?)")


def parse_duration(text: str) -> float:
    """'0:03:25.120' -> 205.12"""
    try:
        h, m, s = text.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return 0.0


def parse_didl(xml_text: str) -> Tuple[List[Tuple[str, str]], List[Track]]:
    """([(container id, title)], [audio tracks]) from a DIDL-Lite browse result."""
    root = ET.fromstring(xml_text)
    containers = [(c.get("id", ""), c.findtext("dc:title", default="?", namespaces=NS))
                  for c in root.findall("didl:container", NS)]
    tracks: List[Track] = []
    for it in root.findall("didl:item", NS):
        cls = it.findtext("upnp:class", default="", namespaces=NS)
        if not cls.startswith("object.item.audioItem"):
            continue
        res = it.find("didl:res", NS)
        if res is None or not (res.text or "").strip():
            continue
        tracks.append({
            "id": it.get("id", ""), "title": it.findtext("dc:title", default="", namespaces=NS),
            "artist": it.findtext("upnp:artist", default="", namespaces=NS)
                      or it.findtext("dc:creator", default="", namespaces=NS),
            "album": it.findtext("upnp:album", default="", namespaces=NS),
            "duration": parse_duration(res.get("duration", "")), "url": res.text.strip(),
        })
    return containers, tracks


class DlnaLibrary(NetLibrary):
    kind = "dlna"

    def __init__(self, name: str, location: str = "", server: str = "", start: str = "") -> None:
        super().__init__(name, location or server or name)
        self.location, self.server, self.start = location, server, start.strip("/")
        self.control = ""

    def _connect(self) -> None:
        candidates = [self.location] if self.location else discover()
        if not candidates:
            raise http.NetError("no DLNA server answered (set `location` to its description URL)")
        for loc in candidates:
            try:
                name, control = describe(loc)
            except http.NetError as exc:
                log.debug("skipping %s: %s", loc, exc)
                continue
            if not self.server or self.server.lower() in name.lower():
                log.info("DLNA server %r at %s", name, loc)
                self.location, self.control = loc, control
                return
        raise http.NetError(f"no DLNA server named like {self.server!r} found")

    def browse(self, object_id: str) -> Tuple[List[Tuple[str, str]], List[Track]]:
        """All direct children of a container (paged)."""
        containers: List[Tuple[str, str]] = []
        tracks: List[Track] = []
        start = 0
        while True:
            body = ('<?xml version="1.0" encoding="utf-8"?>'
                    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                    f'<u:Browse xmlns:u="{CONTENT_DIRECTORY}">'
                    f"<ObjectID>{_xml_escape(object_id)}</ObjectID><BrowseFlag>BrowseDirectChildren</BrowseFlag>"
                    f"<Filter>*</Filter><StartingIndex>{start}</StartingIndex>"
                    f"<RequestedCount>{PAGE}</RequestedCount><SortCriteria></SortCriteria>"
                    "</u:Browse></s:Body></s:Envelope>").encode("utf-8")
            raw = http.request(self.control, {"Content-Type": 'text/xml; charset="utf-8"',
                                              "SOAPACTION": f'"{CONTENT_DIRECTORY}#Browse"'}, data=body)
            try:
                env = ET.fromstring(raw)
            except ET.ParseError:
                raise http.NetError("bad Browse answer") from None
            result = next((e.text for e in env.iter() if e.tag.endswith("Result")), None) or ""
            returned = next((e.text for e in env.iter() if e.tag.endswith("NumberReturned")), "0")
            total = next((e.text for e in env.iter() if e.tag.endswith("TotalMatches")), "0")
            if result:
                c, t = parse_didl(result)
                containers += c
                tracks += t
            start += int(returned or 0)
            if int(returned or 0) == 0 or start >= int(total or 0):
                return containers, tracks

    def ping(self) -> None:
        http.request(self.location, timeout=5)

    def _start_id(self) -> Tuple[str, str]:
        """Follow the `start` path of titles (e.g. "Music/Folders") from the root."""
        node, label = "0", ""
        for part in [p for p in self.start.split("/") if p]:
            containers, _ = self.browse(node)
            match = [c for c in containers if c[1].lower() == part.lower()]
            if not match:
                raise http.NetError(f"DLNA: no folder {part!r} under {label or 'the root'} "
                                    f"(found: {', '.join(t for _, t in containers[:8])})")
            node, label = match[0][0], f"{label}/{part}" if label else part
        return node, label

    def list_folders(self) -> List[Tuple[str, str]]:
        if not self.control:
            self._connect()
        start_id, _ = self._start_id()
        out: List[Tuple[str, str]] = []
        queue = deque([(start_id, "")])
        seen = 0
        while queue and seen < MAX_CONTAINERS:
            cid, path = queue.popleft()
            seen += 1
            containers, tracks = self.browse(cid)
            if tracks:
                out.append((cid, path or self.name))
                self._tracks[cid] = tracks       # got them anyway - no second request later
            for child_id, title in containers:
                queue.append((child_id, f"{path} / {title}" if path else title))
        if queue:
            log.warning("DLNA: stopped after %d containers - set `start` to a smaller view", MAX_CONTAINERS)
        return out

    def list_tracks(self, key: str) -> List[Track]:
        return self.browse(key)[1]


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
