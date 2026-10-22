"""URL canonicalisation, handle normalisation, link and email extraction."""
from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from receipts.util.text import sentence_around


@lru_cache(maxsize=1)
def _tld():
    import tldextract

    # bundled public-suffix snapshot only: no network fetch, works in fixture mode
    return tldextract.TLDExtract(suffix_list_urls=())


def canonical_url(url: str) -> str:
    """Lowercase host, strip www., trailing slash, utm_* params and fragment."""
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host + (f":{p.port}" if p.port and p.port not in (80, 443) else "")
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    path = p.path.rstrip("/")
    return urlunsplit(((p.scheme or "https").lower(), netloc, path, urlencode(q), ""))


def host_of(url: str) -> str:
    h = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def registrable_domain(url: str) -> str:
    ext = _tld()(url if "://" in url else "https://" + url)
    if not ext.domain or not ext.suffix:
        return host_of(url)  # unknown suffix (e.g. .test, intranet): the whole host is the identity
    return f"{ext.domain}.{ext.suffix}".lower()


def is_shared_host(url: str, shared: list[str]) -> bool:
    host = host_of(url)
    reg = registrable_domain(url)
    for s in shared:
        s = s.lower()
        if host == s or reg == s or host.endswith("." + s):
            return True
    return False


_HANDLE_HOSTS = {
    "youtube": ("youtube.com", "youtu.be", "m.youtube.com"),
    "instagram": ("instagram.com",),
    "x": ("x.com", "twitter.com"),
    "linkedin": ("linkedin.com",),
}


def normalise_handle(platform: str, value: str) -> str:
    """'@Name', 'name', 'https://youtube.com/@Name/' -> 'name'. LinkedIn -> 'in/<slug>' or 'company/<slug>'."""
    v = unquote((value or "").strip())
    if "://" in v or any(h in v.lower() for h in _HANDLE_HOSTS.get(platform, ())):
        path = urlsplit(v if "://" in v else "https://" + v).path.strip("/")
        parts = [p for p in path.split("/") if p]
        if platform == "linkedin":
            if len(parts) >= 2 and parts[0].lower() in ("in", "company", "school"):
                return f"{parts[0].lower()}/{parts[1].lower()}"
            return path.lower()
        if platform == "youtube" and parts:
            if parts[0].startswith("@"):
                return parts[0][1:].lower()
            if parts[0] in ("c", "user") and len(parts) > 1:
                return parts[1].lower()
            if parts[0] == "channel" and len(parts) > 1:
                return parts[1]  # channel ids are case-sensitive
            return parts[0].lower()
        v = parts[0] if parts else ""
    return v.lstrip("@").strip().lower()


def extract_links(html: str, base_url: str) -> list[str]:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser

    out, seen = [], set()
    for a in HTMLParser(html or "").css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        u = urljoin(base_url, href)
        if u.startswith(("http://", "https://")) and u not in seen:
            seen.add(u)
            out.append(u)
    return out


_EMAIL = re.compile(r"(?<![\w.+-])([A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24})(?![\w-])")
# only the bracketed form counts as "clearly formatted": name [at] domain [dot] com / (at) (dot)
_OBF = re.compile(
    r"(?<![\w.])([A-Za-z0-9][A-Za-z0-9._%+-]{0,63})\s*[\[(]\s*at\s*[\])]\s*"
    r"([A-Za-z0-9-]+(?:\s*[\[(]\s*dot\s*[\])]\s*[A-Za-z0-9-]+)+)",
    re.IGNORECASE,
)
_NOT_TLD = {"png", "jpg", "jpeg", "gif", "webp", "svg", "css", "js", "ico", "bmp", "tiff", "mp4", "mov"}


def deobfuscate(text: str) -> str:
    """Rewrite only bracketed '[at]'/'[dot]' forms into plain addresses."""
    def fix(m: re.Match) -> str:
        dom = re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", m.group(2), flags=re.IGNORECASE)
        return f"{m.group(1)}@{dom}"

    return _OBF.sub(fix, text or "")


def extract_emails(text: str) -> list[tuple[str, str]]:
    """(email, surrounding quote <=200 chars). Never constructs addresses not literally present."""
    found: dict[str, str] = {}
    text = text or ""
    for m in _EMAIL.finditer(text):
        e = m.group(1).rstrip(".")
        if e.rsplit(".", 1)[-1].lower() in _NOT_TLD:
            continue
        found.setdefault(e.lower(), sentence_around(text, m.start(1), m.end(1)))
    for m in _OBF.finditer(text):
        e = deobfuscate(m.group(0)).strip().lower()
        if _EMAIL.fullmatch(e) and e not in found:
            found[e] = sentence_around(text, m.start(), m.end())
    return list(found.items())


def youtube_channel_url(channel_id: str) -> str:
    return f"https://www.youtube.com/channel/{channel_id}"


def youtube_video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"
