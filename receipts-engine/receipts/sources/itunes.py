"""iTunes lookup, cross-check only (feed URL and show name). Store country is not creator country."""
from __future__ import annotations

from receipts.http import HttpClient


async def lookup(http: HttpClient, itunes_id: int | str) -> dict | None:
    r = await http.get("https://itunes.apple.com/lookup", params={"id": itunes_id, "entity": "podcast"},
                       kind="api", expect="json")
    if not r.ok:
        return None
    res = r.json().get("results", [])
    if not res:
        return None
    x = res[0]
    return {"name": x.get("collectionName"), "artist": x.get("artistName"), "feed_url": x.get("feedUrl"),
            "track_count": x.get("trackCount"), "genre": x.get("primaryGenreName"), "snap_id": r.snap_id}
