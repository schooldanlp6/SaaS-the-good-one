"""Network media: Jellyfin, Subsonic API servers, DLNA/UPnP media servers.

Each server is one medium (cycled with `*` like a stick): its folders are the
server's albums (or DLNA containers), its tracks are stream URLs mpv plays.
Standard library only (urllib, xml.etree, socket)."""
