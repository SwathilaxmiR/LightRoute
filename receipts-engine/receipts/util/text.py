"""Text normalisation, quote verification, language detection."""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_QUOTES = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "´": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"', "«": '"', "»": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
    " ": " ",
})
_WS = re.compile(r"\s+")


def normalise(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.translate(_QUOTES)
    s = _WS.sub(" ", s)
    return s.casefold().strip()


def verify_quote(quote: str, snapshot_text: str, min_chars: int = 12) -> bool:
    q = normalise(quote)
    if len(q) < min_chars:
        return False
    return q in normalise(snapshot_text)


def word_count(s: str) -> int:
    return len(re.findall(r"[\w'’-]+", s or ""))


@lru_cache(maxsize=1)
def _detector():
    from lingua import LanguageDetectorBuilder

    # high-accuracy mode: low-accuracy mode misreads short English titles (e.g. as Polish)
    return LanguageDetectorBuilder.from_all_languages().build()


def detect_language(texts: list[str], min_confidence: float = 0.1, min_letters: int = 10) -> list[str]:
    """ISO 639-1 code per text; 'und' when the text is too short or lingua is not confident.

    Confidence is spread over ~75 languages, so the top value is compared to a low floor.
    """
    det = _detector()
    out = []
    for t in texts:
        t = (t or "").strip()
        if sum(ch.isalpha() for ch in t) < min_letters:
            out.append("und")
            continue
        vals = det.compute_language_confidence_values(t)
        if not vals or vals[0].value < min_confidence:
            out.append("und")
        else:
            out.append(vals[0].language.iso_code_639_1.name.lower())
    return out


def english_share(texts: list[str], min_confidence: float = 0.1, min_letters: int = 10) -> tuple[int, int]:
    langs = detect_language(texts, min_confidence, min_letters)
    return sum(1 for lang in langs if lang == "en"), len(langs)


def extract_main_text(html: str) -> str:
    try:
        import trafilatura

        txt = trafilatura.extract(html, include_comments=False, include_tables=True, favor_recall=True)
    except Exception:  # trafilatura can choke on odd markup; fall back
        txt = None
    return txt or visible_text(html)


def visible_text(html: str) -> str:
    from selectolax.lexbor import LexborHTMLParser as HTMLParser

    tree = HTMLParser(html or "")
    for node in tree.css("script, style, noscript, template"):
        node.decompose()
    body = tree.body or tree.root
    return body.text(separator=" ", strip=True) if body else ""


def sentence_around(text: str, start: int, end: int, max_chars: int = 200) -> str:
    """The span [start,end) widened to sentence-ish boundaries, capped at max_chars."""
    left = max(0, start - (max_chars - (end - start)) // 2)
    right = min(len(text), end + (max_chars - (end - start)) // 2)
    seg_start = max(text.rfind(". ", left, start), text.rfind("\n", left, start))
    seg_start = seg_start + 1 if seg_start >= 0 else left
    cands = [i for i in (text.find(". ", end, right), text.find("\n", end, right)) if i >= 0]
    seg_end = min(cands) + 1 if cands else right
    return text[seg_start:seg_end].strip()
