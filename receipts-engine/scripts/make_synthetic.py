"""Synthetic fixtures for tests, the keyless demo and bench ONLY (BUILD_SPEC rule 0.3).

Everything produced here is unmistakably fake: names start with "Synthetic", domains end in
the reserved `.test` TLD, channel ids start with "UCsynth". Every fixture file carries
origin="synthetic", so replayed snapshots get data_origin='synthetic' and the exporter (G1)
refuses them for out/leads.csv.

Usage: python scripts/make_synthetic.py --out tests/fixtures/synthetic --creators 12
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

from receipts.http import write_fixture_file
from receipts.sources.youtube import BASE as YT
from receipts.sources.youtube import VIDEO_PARTS_FULL

PI = "https://api.podcastindex.org/api/1.0"


# ---------------------------------------------------------------- shape builders
def iso_dur(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "PT" + (f"{h}H" if h else "") + (f"{m}M" if m else "") + (f"{s}S" if s or not (h or m) else "")


def yt_channel_item(cid: str, title: str, description: str, country: str | None, subs: int | None,
                    uploads: str, custom_url: str = "", video_count: int = 120, keywords: str = "") -> dict:
    stats = {"viewCount": "1000", "videoCount": str(video_count), "hiddenSubscriberCount": subs is None}
    if subs is not None:
        stats["subscriberCount"] = str(subs)
    sn = {"title": title, "description": description, "customUrl": custom_url, "publishedAt": "2019-01-01T00:00:00Z"}
    if country:
        sn["country"] = country
    return {"kind": "youtube#channel", "id": cid, "snippet": sn, "statistics": stats,
            "contentDetails": {"relatedPlaylists": {"uploads": uploads}},
            "brandingSettings": {"channel": {"title": title, "description": description, "keywords": keywords}}}


def yt_video_item(vid: str, title: str, description: str, published: datetime, seconds: int,
                  caption: bool = False, paid: bool = False) -> dict:
    return {"kind": "youtube#video", "id": vid,
            "snippet": {"publishedAt": published.strftime("%Y-%m-%dT%H:%M:%SZ"), "title": title,
                        "description": description, "liveBroadcastContent": "none", "defaultAudioLanguage": "en"},
            "contentDetails": {"duration": iso_dur(seconds), "caption": "true" if caption else "false"},
            "statistics": {"viewCount": "100"}, "status": {"privacyStatus": "public"},
            "paidProductPlacementDetails": {"hasPaidProductPlacement": paid}}


def rss_xml(title: str, link: str, items: list[dict], owner_email: str = "", owner_name: str = "",
            language: str = "en-us", description: str = "") -> str:
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
        'xmlns:podcast="https://podcastindex.org/namespace/1.0">',
        f"<channel><title>{title}</title><link>{link}</link><language>{language}</language>",
        f"<description>{description}</description>",
        f"<itunes:owner><itunes:name>{owner_name}</itunes:name><itunes:email>{owner_email}</itunes:email></itunes:owner>",
    ]
    for it in items:
        out.append(
            f"<item><title>{it['title']}</title><link>{it['link']}</link><guid>{it['guid']}</guid>"
            f"<pubDate>{format_datetime(it['published'])}</pubDate>"
            f"<itunes:duration>{it['duration']}</itunes:duration>"
            f"<description><![CDATA[{it.get('description', '')}]]></description>"
            + ('<podcast:transcript url="' + it['link'] + '/t.vtt" type="text/vtt"/>' if it.get("transcript") else "")
            + f'<enclosure url="{it["link"]}/audio.mp3" type="audio/mpeg" length="1"/></item>'
        )
    out.append("</channel></rss>")
    return "\n".join(out)


def html_page(title: str, body: str, jsonld: dict | None = None) -> str:
    ld = f'<script type="application/ld+json">{json.dumps(jsonld)}</script>' if jsonld else ""
    return f"<!doctype html><html><head><title>{title}</title>{ld}</head><body>{body}</body></html>"


# ---------------------------------------------------------------- fixture writers
def register_youtube_channel(fx: Path, ch: dict, videos: list[dict]) -> None:
    cid = ch["id"]
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    write_fixture_file(fx, f"{YT}/channels", {"part": "snippet,statistics,brandingSettings,contentDetails",
                                              "id": cid, "maxResults": 50}, json.dumps({"items": [ch]}))
    pl = {"items": [{"contentDetails": {"videoId": v["id"], "videoPublishedAt": v["snippet"]["publishedAt"]}}
                    for v in videos[:50]]}
    write_fixture_file(fx, f"{YT}/playlistItems", {"part": "contentDetails", "playlistId": uploads, "maxResults": 50},
                       json.dumps(pl))
    ids = [v["id"] for v in videos[:50]]
    write_fixture_file(fx, f"{YT}/videos", {"part": VIDEO_PARTS_FULL, "id": ",".join(ids), "maxResults": 50},
                       json.dumps({"items": videos[:50]}))
    for v in ids[:3]:
        write_fixture_file(fx, f"{YT}/commentThreads", {"part": "snippet,replies", "videoId": v, "maxResults": 20,
                                                        "order": "time"}, json.dumps({"items": []}))


def register_youtube_search(fx: Path, query: str, channel_ids: list[str], max_results: int = 50) -> None:
    write_fixture_file(fx, f"{YT}/search", {"part": "snippet", "type": "channel", "q": query,
                                            "maxResults": max_results, "relevanceLanguage": "en"},
                       json.dumps({"items": [{"id": {"kind": "youtube#channel", "channelId": c}} for c in channel_ids]}))


def register_pi_feed(fx: Path, feed: dict, episodes: list[dict]) -> None:
    write_fixture_file(fx, f"{PI}/podcasts/byfeedid", {"id": feed["id"]}, json.dumps({"status": "true", "feed": feed}))
    write_fixture_file(fx, f"{PI}/episodes/byfeedid", {"id": feed["id"], "max": 100},
                       json.dumps({"status": "true", "items": episodes}))


def register_pi_search(fx: Path, q: str, feeds: list[dict], max_: int = 50) -> None:
    write_fixture_file(fx, f"{PI}/search/byterm", {"q": q, "max": max_}, json.dumps({"status": "true", "feeds": feeds}))


def register_page(fx: Path, url: str, html: str, status: int = 200) -> None:
    write_fixture_file(fx, url, None, html, status=status, content_type="text/html; charset=utf-8")


def register_feed(fx: Path, url: str, xml: str) -> None:
    write_fixture_file(fx, url, None, xml, content_type="application/rss+xml")


def register_robots(fx: Path, origin: str, txt: str | None) -> None:
    if txt is None:
        write_fixture_file(fx, origin + "/robots.txt", None, "", status=404, content_type="text/plain")
    else:
        write_fixture_file(fx, origin + "/robots.txt", None, txt, content_type="text/plain")


def register_mx(fx: Path, domain: str, hosts: list[str]) -> None:
    d = Path(fx) / "dns"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{domain}.json").write_text(json.dumps({"origin": "synthetic", "domain": domain, "mx": hosts}))


# ---------------------------------------------------------------- synthetic world
@dataclass
class SynthCreator:
    n: int
    kind: str                     # youtube | podcast | website
    country: str = "US"
    place: str = "Austin, Texas"
    english: bool = True
    active: bool = True
    shorts: bool = False
    captions: bool = False
    commercial: bool = True
    agency: bool = False
    video_count: int = 140
    subs: int | None = 42000
    email: bool = True
    linkedin: bool = False
    expert: bool = False          # coaching offer on site + sub-8-minute videos -> expert_led segment
    extra: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return f"Synthetic Creator {self.n:04d}"

    @property
    def slug(self) -> str:
        return f"synthcreator{self.n:04d}"

    @property
    def site(self) -> str:
        return f"https://{self.slug}.test"

    @property
    def channel_id(self) -> str:
        return "UCsynth" + f"{self.n:017d}"  # 24 chars like a real channel id

    @property
    def feed_id(self) -> int:
        return 9_000_000 + self.n


def build_world(fx: Path, creators: list[SynthCreator], now: datetime) -> None:
    for c in creators:
        _build_site(fx, c)
        if c.kind == "youtube":
            _build_youtube(fx, c, now)
        elif c.kind == "podcast":
            _build_podcast(fx, c, now)
    (Path(fx) / "SYNTHETIC_README.txt").write_text(
        "Every file in this directory is synthetic test data generated by scripts/make_synthetic.py.\n"
        "It describes no real person or business. Replayed snapshots are data_origin='synthetic'.\n")


def _titles(c: SynthCreator) -> list[str]:
    if c.english:
        return [f"How to price your first offer, part {i}" for i in range(1, 60)]
    return [f"Cómo fijar el precio de tu primera oferta, parte {i}" for i in range(1, 60)]


def _build_site(fx: Path, c: SynthCreator) -> None:
    register_robots(fx, c.site, "User-agent: *\nDisallow: /private\n")
    links = '<a href="/about">About</a> <a href="/contact">Contact</a> <a href="/courses">Courses</a>'
    if c.linkedin:
        links += f' <a href="https://www.linkedin.com/in/{c.slug}">LinkedIn</a>'
    if c.commercial:
        links += f' <a href="https://{c.slug}.kajabi.com/offers/x">Join my course</a>'
    who = "We are a marketing agency. Our clients include brands." if c.agency else \
        f"I'm {c.name}, an independent educator based in {c.place}."
    if c.expert:
        who += " Book a free call to start 1:1 coaching with me."
    register_page(fx, c.site, html_page(c.name, f"<h1>{c.name}</h1><p>{who}</p><nav>{links}</nav>",
                                        {"@type": "Person", "name": c.name,
                                         "address": {"@type": "PostalAddress", "addressCountry": c.country}}))
    register_page(fx, c.site + "/about", html_page("About", f"<p>{who} I publish a new long episode every week.</p>"))
    contact = (f"<p>For business inquiries and sponsorships: hello@{c.slug}.test</p>" if c.email else "<p>Say hi!</p>")
    register_page(fx, c.site + "/contact", html_page("Contact", contact +
                                                     '<form action="/send"><input name="m"></form>'))
    register_page(fx, c.site + "/courses", html_page("Courses", "<p>My flagship course: Pricing Basics.</p>"))
    if c.email:
        register_mx(fx, f"{c.slug}.test", [f"mx1.{c.slug}.test"])


def _build_youtube(fx: Path, c: SynthCreator, now: datetime) -> None:
    gap = 3 if c.active else 70
    vids = []
    titles = _titles(c)
    for i in range(30):
        long_ = i % 3 != 2 or not c.shorts
        secs = (300 if c.expert else 1500) if long_ else 45
        desc = f"Episode notes. Website: {c.site}"
        if c.commercial and i == 0:
            desc += " This video is sponsored by Example Widgets. Use code SYNTH for 10% off."
        vids.append(yt_video_item(f"synv{c.n:04d}{i:03d}", titles[i], desc, now - timedelta(days=1 + i * gap),
                                  secs, caption=c.captions))
    title = f"{c.name} Agency" if c.agency else c.name
    desc = f"Weekly lessons on pricing and offers. Based in {c.place}. Business: hello@{c.slug}.test {c.site}" \
        if c.email else f"Weekly lessons on pricing and offers. Based in {c.place}. {c.site}"
    ch = yt_channel_item(c.channel_id, title, desc, c.country, c.subs, "UU" + c.channel_id[2:],
                         "@" + c.slug, c.video_count)
    register_youtube_channel(fx, ch, vids)


def _build_podcast(fx: Path, c: SynthCreator, now: datetime) -> None:
    feed_url = f"{c.site}/feed.xml"
    gap = 7 if c.active else 70
    eps = []
    for i in range(40):
        pub = now - timedelta(days=2 + i * gap)
        eps.append({"title": _titles(c)[i], "link": f"{c.site}/ep/{i}", "guid": f"{c.slug}-ep-{i}", "published": pub,
                    "duration": "00:42:10", "transcript": c.captions,
                    "description": ("Brought to you by Example Widgets. " if c.commercial and i == 0 else "")
                    + "Show notes for this episode."})
    register_robots(fx, c.site, "User-agent: *\nDisallow: /private\n")
    register_feed(fx, feed_url, rss_xml(c.name, c.site, eps, owner_email=f"hello@{c.slug}.test", owner_name=c.name,
                                        language="en-us" if c.english else "es",
                                        description=f"A show about pricing, recorded in {c.place}."))
    feed = {"id": c.feed_id, "title": c.name, "url": feed_url, "link": c.site, "description": "A show about pricing.",
            "author": c.name, "ownerName": c.name, "language": "en" if c.english else "es",
            "lastUpdateTime": int(eps[0]["published"].timestamp()), "episodeCount": len(eps),
            "newestItemPublishTime": int(eps[0]["published"].timestamp()), "itunesId": None,
            "categories": {"9": "Business"}, "dead": 0}
    pis = [{"id": 70_000_000 + c.n * 100 + i, "title": e["title"], "link": e["link"],
            "description": e["description"], "datePublished": int(e["published"].timestamp()), "duration": 2530,
            "enclosureUrl": e["link"] + "/audio.mp3"} for i, e in enumerate(eps)]
    register_pi_feed(fx, feed, pis)


def default_world() -> list[SynthCreator]:
    """A small mixed world covering pass and fail paths."""
    places = {"US": "Austin, Texas", "GB": "London, England", "CA": "Toronto, Canada", "AU": "Sydney, Australia",
              "IE": "Dublin, Ireland", "NZ": "Auckland, New Zealand", "SG": "Singapore"}
    out = []
    countries = list(places)
    for i in range(1, 13):
        cc = countries[i % len(countries)]
        out.append(SynthCreator(n=i, kind="youtube" if i % 2 else "podcast", country=cc, place=places[cc]))
    for i in range(17, 23):
        cc = countries[i % len(countries)]
        out.append(SynthCreator(n=i, kind="youtube", country=cc, place=places[cc], expert=True))
    out += [
        SynthCreator(n=13, kind="youtube", agency=True),
        SynthCreator(n=14, kind="youtube", english=False, country="US"),
        SynthCreator(n=15, kind="youtube", active=False),
        SynthCreator(n=16, kind="podcast", country="IN", place="Mumbai, India"),
    ]
    return out


def input_ref(c: SynthCreator) -> str:
    if c.kind == "youtube":
        return f"https://www.youtube.com/channel/{c.channel_id}"
    if c.kind == "podcast":
        return f"pi:{c.feed_id}"
    return c.site


def write_sample_input(path: Path, world: list[SynthCreator]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(["url"] + [input_ref(c) for c in world]) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tests/fixtures/synthetic")
    a = ap.parse_args()
    world = default_world()
    build_world(Path(a.out), world, datetime.now(UTC))
    write_sample_input(Path(a.out) / "sample_input.csv", world)
    print(f"wrote synthetic world of {len(world)} creators to {a.out}")


if __name__ == "__main__":
    main()
