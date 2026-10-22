"""Creator-owned website crawl: homepage plus up to 8 relevant internal pages."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from receipts.cache import SnapshotStore
from receipts.guards import RobotsDisallowed
from receipts.http import TRANSIENT, FixtureMissing, HttpClient
from receipts.util.text import visible_text
from receipts.util.urls import canonical_url, extract_links, host_of

PAGE_PATTERN = re.compile(
    r"/(about|contact|work-with|sponsor|advertise|partner|courses?|coaching|shop|store|press|media-kit|guest|pitch|team)",
    re.IGNORECASE,
)
MAX_SUBPAGES = 8  # BUILD_SPEC 8.8; override with discovery.site_max_pages
FORM_EMBEDS = ("typeform.com", "jotform.com", "docs.google.com/forms", "forms.gle", "hsforms", "hubspot.com/forms",
               "tally.so", "formstack", "wufoo.com", "cognitoforms", "123formbuilder", "calendly.com", "airtable.com/shr",
               "paperform.co", "formspree.io", "netlify")


async def _fetch_page(client: HttpClient, url: str) -> tuple[int, str] | None:
    try:
        r = await client.get(url, kind="web")
    except (RobotsDisallowed, FixtureMissing):
        return None
    except TRANSIENT:
        return None
    if not r.ok or r.snap_id is None or "html" not in (r.content_type or "html").lower():
        return None
    return r.snap_id, visible_text(r.text)


async def fetch_site(base_url: str, client: HttpClient, max_pages: int = MAX_SUBPAGES) -> dict[str, tuple[int, str]]:
    """url -> (snap_id, visible text). Raw HTML is the snapshot itself."""
    pages: dict[str, tuple[int, str]] = {}
    home = await _fetch_page(client, base_url)
    if home is None:
        return pages
    pages[base_url] = home
    store = client.store
    raw = store.load_raw(home[0]).decode("utf-8", errors="replace") if store else ""
    site_host = host_of(base_url)
    chosen: list[str] = []
    seen = {canonical_url(base_url)}
    for link in extract_links(raw, base_url):
        if host_of(link) != site_host:
            continue
        c = canonical_url(link)
        if c in seen:
            continue
        if PAGE_PATTERN.search(urlsplit(link).path):
            seen.add(c)
            chosen.append(link.split("#")[0])
        if len(chosen) >= max_pages:
            break
    for link in chosen:
        got = await _fetch_page(client, link)
        if got:
            pages[link] = got
    return pages


def page_links(store: SnapshotStore, snap_id: int, base_url: str | None = None) -> list[str]:
    row = store.get(snap_id)
    raw = store.load_raw(snap_id).decode("utf-8", errors="replace")
    return extract_links(raw, base_url or (row["url"] if row else ""))


def page_html(store: SnapshotStore, snap_id: int) -> str:
    return store.load_raw(snap_id).decode("utf-8", errors="replace")


def has_form(html: str) -> bool:
    tree = HTMLParser(html or "")
    if tree.css_first("form"):
        return True
    for node in tree.css("iframe[src], script[src], a[href]"):
        src = (node.attributes.get("src") or node.attributes.get("href") or "").lower()
        if any(e in src for e in FORM_EMBEDS if e != "calendly.com"):
            return True
    return False


def parse_schema_org(html: str) -> dict:
    """JSON-LD Organization/Person/Place: addressCountry values, sameAs links, @types."""
    out: dict = {"addressCountry": [], "sameAs": [], "types": []}
    for node in HTMLParser(html or "").css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
                continue
            if not isinstance(d, dict):
                continue
            t = d.get("@type")
            if t:
                out["types"] += t if isinstance(t, list) else [t]
            sa = d.get("sameAs")
            if sa:
                out["sameAs"] += sa if isinstance(sa, list) else [sa]
            addr = d.get("address")
            for a in (addr if isinstance(addr, list) else [addr]):
                if isinstance(a, dict) and a.get("addressCountry"):
                    c = a["addressCountry"]
                    out["addressCountry"].append(c.get("name") if isinstance(c, dict) else c)
            if d.get("addressCountry"):
                out["addressCountry"].append(d["addressCountry"])
            for k in ("@graph", "author", "founder", "publisher", "location", "brand"):
                if isinstance(d.get(k), (dict, list)):
                    stack.append(d[k])
    return out
