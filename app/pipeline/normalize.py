"""Normalisation: ``RawJob`` -> ``Job``.

Responsibilities: safe HTML stripping, remote detection, salary extraction,
geo-restriction detection. No network, no scoring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape

from bs4 import BeautifulSoup

from app.models import GeoScope, Job, RawJob, canonical_url

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u00a0]+")
_NL_RE = re.compile(r"\n{3,}")

REMOTE_TOKENS = (
    "remote",
    "удаленн",
    "distance",
    "anywhere",
    "work from home",
    "distributed",
)
ONSITE_TOKENS = ("on-site", "onsite", "в офисе", "гибрид", "hybrid")

#: Phrases that mean "you must already live somewhere specific".
GEO_RESTRICT_RE = re.compile(
    r"\b(must be located in|must be based in|only (?:candidates )?(?:located|based)"
    r"|us[- ]only|us based|u\.s\. based|only in the us|eu[- ]only|cet[- ]only"
    r"|based in (?:the )?(?:us|usa|united states|uk|eu|europe|germany|netherlands|poland))\b",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------- #
# geo scope: "remote" is not one thing
#
# The customer lives in Europe and wants *worldwide* remote. An ATS field that
# says "Remote - USA" (Coinbase lists 134 of them) is a US-only job, and a bare
# city ("Singapore", "Vienna") is an office job with a remote-ish label. The
# classifier below reads the location field first (it is short and structured),
# then falls back to explicit statements in the description.
# --------------------------------------------------------------------------- #

#: Location strings that mean "work from anywhere".
WORLDWIDE_TOKENS = ("remote", "worldwide", "world-wide", "anywhere", "global", "distributed")

#: Countries/regions that include the customer's residence -> no penalty.
EUROPE_TOKENS = (
    "europe",
    "european",
    "emea",
    "eu",
    "u.e.",
    "cet",
    "gmt",
    "uk",
    "u.k.",
    "united kingdom",
    "britain",
    "england",
    "london",
    "germany",
    "berlin",
    "netherlands",
    "amsterdam",
    "poland",
    "warsaw",
    "ukraine",
    "kyiv",
    "spain",
    "madrid",
    "barcelona",
    "portugal",
    "lisbon",
    "italy",
    "milan",
    "rome",
    "austria",
    "vienna",
    "czech",
    "prague",
    "lithuania",
    "vilnius",
    "estonia",
    "tallinn",
    "latvia",
    "riga",
    "switzerland",
    "zurich",
    "france",
    "paris",
    "ireland",
    "dublin",
    "finland",
    "helsinki",
    "sweden",
    "stockholm",
    "denmark",
    "copenhagen",
    "belgium",
    "brussels",
    "romania",
    "bulgaria",
    "serbia",
    "belgrade",
    "georgia",
    "tbilisi",
    "armenia",
    "yerevan",
    "cyprus",
    "limassol",
    "moldova",
    "cis",
    "russia",
    "moscow",
)

#: Broad regions that do *not* include Europe.
REGION_TOKENS = (
    "apac",
    "a-pac",
    "asia",
    "asian",
    "pacific",
    "anzac",
    "latam",
    "latin america",
    "americas",
    "north america",
    "south america",
    "mena",
    "middle east",
)

#: The USA in every spelling an ATS board uses, plus US states ("New York, NY").
USA_TOKENS = frozenset(
    {
        "usa", "us", "u.s.", "u.s.a.", "u.s.a", "united states", "u.s. remote",
        "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il",
        "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt",
        "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri",
        "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc",
    }
)

#: "must be located in the US", "US only", "based in the United States".
_USA_STATEMENT_RE = re.compile(
    r"\b(?:must be (?:located|based) in the (?:us|usa|u\.s\.|united states)"
    r"|us[- ]only|only in the us|us[- ]based|u\.s\.[- ]based"
    r"|based in the (?:us|usa|united states)"
    r"|work(?:ing)? from (?:the )?(?:us|usa|united states))\b",
    re.IGNORECASE,
)
#: "Work from anywhere", "Remote (Worldwide)".
_WORLDWIDE_STATEMENT_RE = re.compile(
    r"\b(work from anywhere|anywhere in the world|remote[- ]?worldwide|worldwide remote"
    r"|remote anywhere|fully remote anywhere)\b",
    re.IGNORECASE,
)

#: Location separators used by ATS boards: "Remote - USA", "Abu Dhabi, UAE; KL".
_LOCATION_SPLIT_RE = re.compile(r"[,;/|()\[\]]+| - | – | — ")


@dataclass(frozen=True, slots=True)
class GeoInfo:
    """Result of :func:`classify_geo` — a scope plus a human-readable place."""

    scope: GeoScope = GeoScope.UNKNOWN
    place: str = ""


def _place_parts(location: str) -> list[str]:
    """Location split into comparable fragments, "remote"-ness removed.

    ``"Remote - Abu Dhabi, UAE"`` -> ``["abu dhabi", "uae"]``: the first real
    place is what the card should show, not the word "Remote".
    """
    parts = [p.strip(" -–—") for p in _LOCATION_SPLIT_RE.split((location or "").strip())]
    return [p for p in parts if p and p.casefold() not in WORLDWIDE_TOKENS]


def _place_name(location: str) -> str:
    """Shortest readable label for a location string ("Abu Dhabi, UAE" -> "Abu Dhabi")."""
    parts = _place_parts(location)
    return (parts[0] if parts else "")[:40]


def _matches(fragment: str, tokens: frozenset[str] | tuple[str, ...]) -> bool:
    """Does ``fragment`` mention one of ``tokens`` at word level?

    Two-word keywords ("united states") are matched as a whole fragment, so a
    city named "Washington" cannot be mistaken for "us".
    """
    words = set(fragment.split())
    for token in tokens:
        if " " in token:
            if fragment == token:
                return True
        elif token in words:
            return True
    return False


def classify_geo(location: str, description: str = "", remote: bool | None = None) -> GeoInfo:
    """Classify how tightly a vacancy is bound to one geography.

    Order matters: an explicit statement in the text ("must be located in the US")
    beats the ATS field, and a country beats the word "Remote" that usually
    prefixes it — "Remote - Singapore" is *not* worldwide. When several places are
    listed, the first one wins ("London, New York" is Europe-friendly).
    """
    head = (description or "")[:2000]
    parts = _place_parts(location)

    if _USA_STATEMENT_RE.search(head):
        return GeoInfo(GeoScope.USA_ONLY, "USA")

    for fragment in parts:
        low = fragment.casefold().strip(". ")
        if _matches(low, USA_TOKENS):
            return GeoInfo(GeoScope.USA_ONLY, "USA")
        if _matches(low, EUROPE_TOKENS):
            return GeoInfo(GeoScope.EUROPE, fragment[:40])
        if _matches(low, REGION_TOKENS):
            return GeoInfo(GeoScope.REGION, fragment[:40])

    norm = " ".join(_LOCATION_SPLIT_RE.sub(" ", (location or "").casefold()).split())
    # "Remote", "Remote - Worldwide", "Anywhere": nothing but remote-ness left.
    if norm and not (set(norm.split()) - set(WORLDWIDE_TOKENS)):
        return GeoInfo(GeoScope.WORLDWIDE, "")
    if remote is not False and _WORLDWIDE_STATEMENT_RE.search(head):
        return GeoInfo(GeoScope.WORLDWIDE, "")

    if not norm:
        return GeoInfo(GeoScope.UNKNOWN, "")
    if remote is False:
        return GeoInfo(GeoScope.LOCAL, _place_name(location))
    return GeoInfo(GeoScope.COUNTRY, _place_name(location))

_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₽": "RUB"}
_CURRENCY_CODES = {"usd", "eur", "gbp", "rub", "uah", "pln", "btc", "eth", "usdt", "usdc"}

#: "$3,000 - $4,500", "3000–4500 USD", "60 000 – 72 000 USD", "€60k", "3-4k"
_MONEY_NUM = r"(?:\d{1,3}(?:[ ,]\d{3})+|\d+(?:\.\d+)?\s?[kK]|\d+(?:\.\d+)?)(?!\d)"
_MONEY_RANGE_RE = re.compile(
    rf"([$€£₽]?)({_MONEY_NUM})\s*(?:[-–—]|to|до)\s*([$€£₽]?)({_MONEY_NUM})\s*"
    r"(usd|eur|gbp|rub|uah|pln|k|)?",
    re.IGNORECASE,
)
_MONEY_SINGLE_RE = re.compile(
    r"([$€£₽])\s*(\d{1,3}(?:[ ,]?\d{3})*(?:\.\d+)?)\s*([kK])?", re.ASCII
)


def strip_html(text: str) -> str:
    """Safely remove markup (BeautifulSoup, no eval, no auto-link fetching).

    Greenhouse (and a few other ATS boards) ship ``content`` as *escaped* HTML
    (``&lt;p&gt;…&lt;/p&gt;``). Unescape first, otherwise the ``<``/``>`` guard
    skips BeautifulSoup and the tags survive into the card.
    """
    if not text:
        return ""
    # Entity-decode repeatedly: some boards double-escape (``&amp;lt;p&amp;gt;``).
    prev = None
    while prev != text:
        prev = text
        text = unescape(text)
    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "html.parser")
        # Keep paragraph / list structure instead of one endless line.
        for tag in soup.find_all(["br", "p", "div", "li", "h1", "h2", "h3", "h4", "tr"]):
            tag.append("\n")
        text = soup.get_text(" ")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    return _NL_RE.sub("\n\n", text).strip()


def detect_remote(*texts: str) -> bool | None:
    """True / False / None(unknown) from any combination of free-text fields."""
    blob = " ".join(t.lower() for t in texts if t)
    if not blob:
        return None
    if any(tok in blob for tok in ONSITE_TOKENS):
        return False
    if any(tok in blob for tok in REMOTE_TOKENS):
        return True
    return None


def _to_number(amount: str, k_suffix: str | None = None) -> float | None:
    digits = re.sub(r"[ ,]", "", amount or "")
    if digits[-1:] in ("k", "K"):  # "60k" — the suffix may ride on the number itself
        k_suffix = k_suffix or digits[-1]
        digits = digits[:-1]
    if not digits:
        return None
    try:
        value = float(digits)
    except ValueError:
        return None
    if k_suffix:
        value *= 1000
    return value


def parse_salary(text: str) -> tuple[float | None, float | None, str]:
    """Best-effort salary extraction.

    Annual-looking figures (>= 15000) are converted to a monthly equivalent so
    they stay comparable with the ``$3–4k`` expectation. Returns
    ``(min, max, currency)``; all ``None``/``""`` when nothing credible found.
    """
    if not text:
        return None, None, ""
    # Only look at lines that smell like compensation, to avoid "2019-2023".
    for line in text.splitlines():
        low = line.lower()
        if not any(c in low for c in _CURRENCY_SYMBOLS) and not any(
            c in low for c in _CURRENCY_CODES
        ):
            continue
        if re.search(r"\b(year|yr|annual|год|yearly|/yr|per year)\b", low) or "год" in low:
            annual = True
        elif re.search(r"\b(month|mo|мес|monthly|/mo)\b", low):
            annual = False
        else:
            annual = None

        m = _MONEY_RANGE_RE.search(line)
        if m:
            raw_lo, raw_hi = m.group(2), m.group(4)
            lo, hi = _to_number(raw_lo), _to_number(raw_hi)
            cur = _resolve_currency(m.group(1) or m.group(3) or m.group(5) or "")
            if _has_k(raw_lo) != _has_k(raw_hi):  # "3-4k" means "3k-4k"
                if lo is not None and not _has_k(raw_lo):
                    lo *= 1000
                if hi is not None and not _has_k(raw_hi):
                    hi *= 1000
            return _finalise(lo, hi, cur, annual)

        singles = _MONEY_SINGLE_RE.findall(line)
        if singles:
            values = [(_to_number(v, k), _resolve_currency(sym)) for sym, v, k in singles]
            values = [(v, c) for v, c in values if v]
            if values:
                lo = min(v for v, _ in values)
                hi = max(v for v, _ in values) if len(values) > 1 else None
                cur = next((c for _, c in values if c), "")
                return _finalise(lo, hi, cur, annual)
    return None, None, ""


def _has_k(token: str) -> bool:
    return token.strip()[-1:].lower() == "k"


def _resolve_currency(raw: str) -> str:
    raw = (raw or "").strip()
    if raw in _CURRENCY_SYMBOLS:
        return _CURRENCY_SYMBOLS[raw]
    return raw.upper() if raw.lower() in _CURRENCY_CODES else ""


def _finalise(
    lo: float | None, hi: float | None, cur: str, annual: bool | None
) -> tuple[float | None, float | None, str]:
    if lo is None and hi is None:
        return None, None, cur
    # Heuristic: a monthly DevOps figure above 15k is almost certainly annual.
    is_annual = annual if annual is not None else max(lo or 0, hi or 0) >= 15000
    if is_annual:
        lo = lo / 12 if lo else None
        hi = hi / 12 if hi else None
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    return round(lo or 0, 2) or None, round(hi or 0, 2) or None, cur


def parse_posted(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=UTC)
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            continue
    try:  # ISO-8601 with offsets
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def normalize(raw: RawJob) -> Job:
    description = strip_html(raw.description)
    location = strip_html(raw.location)

    remote = raw.remote
    if remote is None:
        remote = detect_remote(location, description[:600], raw.title)

    salary_min, salary_max, currency = raw.salary_min, raw.salary_max, raw.currency
    if salary_min is None and salary_max is None:
        salary_min, salary_max, currency = parse_salary(description)
    elif not currency:
        _, _, currency = parse_salary(description)

    job = Job(
        source=raw.source,
        company=strip_html(raw.company).strip() or "—",
        title=strip_html(raw.title).strip() or "—",
        url=canonical_url(raw.url),
        location=location,
        remote=remote,
        salary_min=salary_min,
        salary_max=salary_max,
        currency=currency,
        description=description,
        posted_at=parse_posted(raw.posted_at),
        first_seen_at=datetime.now(tz=UTC),
        raw=raw.raw,
        sources=[raw.source],
    )
    job.geo_restricted = bool(GEO_RESTRICT_RE.search(description[:2000]))
    info = classify_geo(location, description, job.remote)
    job.geo_scope = info.scope
    job.geo_place = info.place
    return job


def age_days(job: Job) -> float:
    stamp = job.posted_at or job.first_seen_at or datetime.now(tz=UTC)
    return max(0.0, (datetime.now(tz=UTC) - stamp).total_seconds() / 86400)


__all__ = [
    "normalize",
    "strip_html",
    "detect_remote",
    "parse_salary",
    "parse_posted",
    "age_days",
    "classify_geo",
    "GeoInfo",
]
