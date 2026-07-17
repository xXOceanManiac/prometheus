"""
prometheus/services/guardian_news.py — Guardian API news service.

Fetches articles from The Guardian, applies Prometheus-specific relevance scoring,
selects the best 10, and pads with fallback data when the API is unavailable.

Config (read from environment at call time — no module-level side effects):
  VITE_GUARDIAN_API_KEY    — Guardian API key
  VITE_GUARDIAN_API_URL    — base URL (default: https://content.guardianapis.com/search)

Usage:
  from prometheus.news.guardian_news import get_news
  articles = get_news()          # returns list of up to 10 normalized article dicts
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional

_DEFAULT_URL = "https://content.guardianapis.com/search"
_FETCH_PAGE_SIZE = 50
_BEST_COUNT = 10


# ── Article normalization ─────────────────────────────────────────────────────

def normalize_article(raw: dict) -> dict:
    """Convert a Guardian API result item into a flat article dict."""
    fields = raw.get("fields") or {}
    trail = str(fields.get("trailText") or "").strip()
    # Strip basic HTML tags from trail text
    import re
    trail = re.sub(r"<[^>]+>", "", trail).strip()
    if len(trail) > 140:
        trail = trail[:137] + "…"

    published_iso = str(raw.get("webPublicationDate") or "").strip()
    thumb_url = str(fields.get("thumbnail") or "").strip()
    return {
        "id": str(raw.get("id") or raw.get("webUrl") or raw.get("webTitle") or ""),
        "title": str(raw.get("webTitle") or fields.get("headline") or "").strip(),
        "href": str(raw.get("webUrl") or "").strip(),
        "tag": str(raw.get("sectionName") or raw.get("sectionId") or "News").strip(),
        "summary": trail,
        "thumb": thumb_url,
        "thumbnail": thumb_url,  # alias so both "thumb" and "thumbnail" keys work
        "byline": str(fields.get("byline") or "").strip(),
        "published_iso": published_iso,
        "time_ago": _time_ago(published_iso),
        "source": "The Guardian",
    }


def _time_ago(iso: str) -> str:
    if not iso:
        return ""
    try:
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        pub = datetime.fromisoformat(iso)
        now = datetime.now(timezone.utc)
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        diff = int((now - pub).total_seconds())
        if diff < 60:
            return "just now"
        if diff < 3600:
            return f"{diff // 60}m ago"
        if diff < 86400:
            return f"{diff // 3600}h ago"
        if diff < 86400 * 30:
            return f"{diff // 86400}d ago"
        return f"{diff // (86400 * 30)}mo ago"
    except Exception:
        return ""


# ── Prometheus relevance scoring ──────────────────────────────────────────────

_HIGH_SIGNAL = [
    # AI / Software / Developer tools
    "artificial intelligence", "ai agent", "llm", "openai", "claude", "gemini", "language model",
    "automation", "software", "coding", "developer", "open source", "algorithm", "machine learning",
    "data center", "semiconductor", "chips", "robotics", "computing", "vibe coding",
    # Science / Engineering / Space
    "space", "nasa", "physics", "quantum", "nuclear energy", "engineering",
    "biology", "medicine", "research", "breakthrough", "science",
    # Education / Microschools / School choice
    "microschool", "school choice", "homeschool", "charter school", "edtech",
    "education reform", "learning", "curriculum", "alternative school",
    # Entrepreneurship / Startups / Product building
    "startup", "founder", "entrepreneur", "venture capital", "ipo",
    "marketplace", "directory", "saas", "bootstrapped", "build in public",
    "small business", "product launch", "private equity",
    # Florida / Arizona / Utah / Sun Belt
    "florida", "south florida", "miami", "orlando", "tallahassee",
    "arizona", "utah", "sun belt",
    # Faith / Culture / Family / Community
    "faith", "church", "family", "parenting", "culture", "tradition", "community", "character",
    # Health / Fitness / Performance
    "fitness", "nutrition", "spartan", "health", "training", "longevity", "sleep", "performance",
    # Design / UI / UX / Creative tools
    "design", "ui design", "ux", "dashboard", "interface design", "creative tools", "architecture",
    # Markets / Economy (practical, not crisis-driven)
    "inflation", "interest rate", "economy", "investment", "supply chain",
    "trade", "recession", "earnings", "revenue", "growth",
    # Gaming / Tech culture (selective)
    "gaming", "xbox", "open source game",
]

_MEDIUM_SIGNAL = [
    # General business
    "company", "ceo", "fund", "infrastructure", "logistics", "pricing", "demand",
    "manufacturing", "exports", "imports", "housing", "acquisition", "merger",
    # Policy / Government (medium, not high, to avoid rewarding crisis coverage)
    "policy", "regulation", "federal", "senate", "governor", "congress", "election",
    "president", "supreme court",
    # Academia / Analysis
    "university", "professor", "study", "analysis", "report", "trend", "research paper",
    # Geopolitics (analytical coverage, boosted further by analysis title logic)
    "geopolitics", "nato", "china", "russia", "middle east",
]

_PENALTY = [
    # Celebrity / clickbait
    "celebrity", "kardashian", "influencer", "dating rumor", "red carpet", "award show",
    # Low-signal sports scores
    "scored against", "match result", "game score",
    # UK-only narrow politics
    "labour party", "conservative party", "tory", "westminster",
    # Sensational framing
    "shocking", "outrage", "slams", "destroys", "explodes at",
]

# Title-level violent/sensational terms — applied to the headline only.
# Each match subtracts 10 points. A single match is enough to nearly eliminate
# a typical article from the top-10 selection.
_VIOLENT_TITLE_TERMS: list[str] = [
    "kill", "killed", "killing", "kills",
    "murder", "murdered",
    "stabbing", "stabbed",
    "shooting", "shot dead",
    "dead body", "bodies found", "corpse",
    "bombing", "bomb blast", "bomb attack",
    "missile strike", "drone strike", "airstrike", "air strike",
    "drone hits", "drone hit",
    "massacre",
    "rape", "sexual assault",
    "horror", "horrifying",
    "brutal", "brutally",
    "bloodshed", "bloodbath",
    "execution", "executed",
    "terror attack", "terrorist attack",
    "hostage",
    "genocide", "ethnic cleansing",
    "famine", "starvation",
    "tragedy", "tragic death",
    "death toll", "casualties",
    # Obituary / death notice title terms
    "obituary",
    "died aged", "dies aged",
    "has died",
    "nuclear disaster",
    "spent nuclear fuel",
]

# ── Hard exclusion ─────────────────────────────────────────────────────────────
# Articles matching these patterns are ALWAYS excluded before relevance ranking.
# Keeps the Prometheus HUD free of obituaries, death notices, and violent
# framing that is not useful or appropriate for a daily mission-control display.

_HARD_EXCLUSION_TITLE_PATTERNS: list[str] = [
    # Obituaries
    "obituary",
    # Death notices
    "dies aged",
    "died aged",
    "dies at ",
    "died at ",
    "has died",
    "found dead",
    "death notice",
    "in memoriam",
    "passed away",
    # Nuclear disaster / violence framing (not energy policy)
    "nuclear disaster",
    "spent nuclear fuel",
    "nuclear fuel near",
    # Direct violent infrastructure hits (headline level)
    "drone hits",
    "drone hit",
    "missile hits",
    "bomb hits",
]

_HARD_EXCLUSION_SECTIONS: list[str] = [
    "obituaries",
]


def _is_disallowed_hud_headline(title: str, section: str, trail_text: str) -> bool:
    """
    Return True if this article should never appear in the Prometheus HUD.

    Applied before relevance ranking. Hard-excludes:
    - Obituaries (section or title pattern)
    - Death notices ("dies aged", "has died", "passed away", etc.)
    - Nuclear disaster / violent drone framing
    """
    title_lower = title.lower()
    section_lower = section.lower()

    for excl in _HARD_EXCLUSION_SECTIONS:
        if excl in section_lower:
            return True

    for pattern in _HARD_EXCLUSION_TITLE_PATTERNS:
        if pattern in title_lower:
            return True

    return False


def prometheus_relevance_score(article: dict) -> float:
    """
    Score an article for relevance to Tate's interests.

    Positive: AI/tech, entrepreneurship, education/microschools, science/space,
              Florida/Arizona/Utah, faith/culture, health/fitness, design/UX,
              practical economics, gaming/tech culture.
    Medium: general business, policy, geopolitics (analytical).
    Penalty: violent/sensational headlines (title-level), celebrity gossip, clickbait.
    Boost: recency, analysis/explainer framing.

    Violent/sensational terms in the title apply a strong -10 penalty each.
    The same terms in the summary apply a lighter -2 penalty (analytical context).
    """
    title = (article.get("title") or "").lower()
    summary = (article.get("summary") or "").lower()
    tag = (article.get("tag") or "").lower()
    hay = f"{title} {summary} {tag}"

    score = 0.0

    # Violent/sensational title penalty — strong; nearly disqualifies the article
    for kw in _VIOLENT_TITLE_TERMS:
        if kw in title:
            score -= 10.0

    # Same terms in summary only — lighter penalty (analytical article may reference these)
    for kw in _VIOLENT_TITLE_TERMS:
        if kw in summary:
            score -= 2.0

    for kw in _HIGH_SIGNAL:
        if kw in hay:
            score += 5.0
    for kw in _MEDIUM_SIGNAL:
        if kw in hay:
            score += 2.0
    for kw in _PENALTY:
        if kw in hay:
            score -= 4.0

    # Recency boost
    iso = article.get("published_iso") or ""
    if iso:
        try:
            if iso.endswith("Z"):
                iso = iso[:-1] + "+00:00"
            pub = datetime.fromisoformat(iso)
            now = datetime.now(timezone.utc)
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
            age_hours = max(0.0, (now - pub).total_seconds() / 3600)
            if age_hours < 6:
                score += 10.0
            elif age_hours < 24:
                score += 6.0
            elif age_hours < 72:
                score += 3.0
        except Exception:
            pass

    # Section tag boosts
    if tag in ("technology", "science", "education"):
        score += 4.0
    elif tag in ("business", "us-news"):
        score += 3.0
    elif tag in ("world", "environment", "politics", "media"):
        score += 1.0

    # Analysis/explainer framing boost — high signal for analytical content
    if any(kw in title for kw in ("explained", "analysis", "what is", "why ", "how ", "guide to", "deep dive")):
        score += 4.0

    return score


# ── Fallback data ─────────────────────────────────────────────────────────────

def _fallback_articles() -> list[dict]:
    """10 Prometheus-specific fallback articles shown when API is unavailable."""
    now_iso = datetime.now(timezone.utc).isoformat()

    def _mk(rank: int, title: str, tag: str, summary: str) -> dict:
        return {
            "id": f"prometheus-demo-{rank}",
            "title": title,
            "href": "https://www.theguardian.com",
            "tag": tag,
            "summary": summary,
            "thumb": "",
            "thumbnail": "",  # alias so both keys are always present
            "byline": "",
            "published_iso": now_iso,
            "time_ago": "just now",
            "source": "Demo",
        }

    return [
        _mk(1, "AI systems in 2026: agents, inference, and the infrastructure race",
            "Technology",
            "The shift from discrete models to always-on agent pipelines is reshaping how software is built — and what automation means."),
        _mk(2, "Calendar-driven automation: the next frontier in personal AI",
            "Technology",
            "Deterministic event triggers replacing LLM polling loops. Implications for latency, reliability, and context-aware routines."),
        _mk(3, "Microschool movement accelerates as families rethink education",
            "Education",
            "Parent-led small-school models are growing across Florida and the Sun Belt, driven by school choice legislation and pandemic-era learning shifts."),
        _mk(4, "Markets update: rates, inflation, and positioning into Q3",
            "Business",
            "Fed commentary, CPI prints, and earnings guidance are driving sector rotation — here's what the data shows this week."),
        _mk(5, "Nuclear, solar, and grid resilience: South Florida energy outlook",
            "Science",
            "FPL's long-range grid plans include modular nuclear and distributed solar, with resilience upgrades targeting Category 5 storm scenarios."),
        _mk(6, "South Florida development: the next wave of mixed-use density",
            "Florida",
            "Brickell, Wynwood, and the urban core continue attracting capital — permitting data, pricing trends, and the 10-year outlook."),
        _mk(7, "Discipline, character, and performance: the Spartan training philosophy",
            "Health",
            "What high-intensity endurance training teaches about consistency, pain tolerance, and building a durable identity around action."),
        _mk(8, "Geopolitics and the technology cold war: where we stand in 2026",
            "World",
            "US-China competition in AI and semiconductors, NATO posture shifts, and the emerging multipolar trade order."),
        _mk(9, "Design and architecture for focus: building environments that perform",
            "Design",
            "How space, light, and acoustic design influence cognitive output — principles from research and practice."),
        _mk(10, "Startup directories and marketplaces: how builders find traction in 2026",
            "Business",
            "Product directories, community-led marketplaces, and niche platforms are replacing cold outreach as the primary discovery channel for indie builders."),
    ]


# ── API fetch ─────────────────────────────────────────────────────────────────

def _load_env_key() -> tuple[str, str]:
    """Return (api_key, base_url) from environment. Never raises."""
    # Also try loading from .env file if key is not in environment
    api_key = os.getenv("VITE_GUARDIAN_API_KEY", "").strip()
    base_url = os.getenv("VITE_GUARDIAN_API_URL", _DEFAULT_URL).strip() or _DEFAULT_URL
    if not api_key:
        # Try reading from project .env file
        try:
            from prometheus.infra.paths import PROJECT_ROOT
            env_path = PROJECT_ROOT / ".env"
        except Exception:
            import pathlib
            env_path = pathlib.Path(__file__).resolve().parent.parent.parent / ".env"
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k == "VITE_GUARDIAN_API_KEY" and not api_key:
                    api_key = v
                elif k == "VITE_GUARDIAN_API_URL" and base_url == _DEFAULT_URL:
                    base_url = v or _DEFAULT_URL
        except Exception:
            pass
    return api_key, base_url


def fetch_guardian_articles(
    api_key: str = "",
    base_url: str = "",
    page_size: int = _FETCH_PAGE_SIZE,
) -> list[dict]:
    """
    Fetch raw Guardian article items.

    Returns list of raw Guardian API result dicts on success.
    Raises RuntimeError with a descriptive message on failure.
    """
    if not api_key or not base_url:
        env_key, env_url = _load_env_key()
        api_key = api_key or env_key
        base_url = base_url or env_url

    if not api_key:
        raise RuntimeError("VITE_GUARDIAN_API_KEY is not set")

    import urllib.parse, urllib.request, json as _json

    params = {
        "api-key": api_key,
        "show-fields": "thumbnail,trailText,headline,byline",
        "order-by": "newest",
        "page-size": str(min(max(1, page_size), 200)),
    }
    url = f"{base_url}?{urllib.parse.urlencode(params)}"

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Guardian API HTTP {exc.code}: {exc.reason}") from exc
    except Exception as exc:
        raise RuntimeError(f"Guardian API fetch failed: {exc}") from exc

    results = data.get("response", {}).get("results", [])
    if not isinstance(results, list):
        raise RuntimeError("Guardian API returned unexpected response shape")
    return results


def pad_to_ten(articles: list[dict]) -> list[dict]:
    """Ensure exactly 10 articles by padding with fallback items."""
    out = list(articles[:_BEST_COUNT])
    if len(out) >= _BEST_COUNT:
        return out
    used = {a["id"] for a in out}
    for fb in _fallback_articles():
        if len(out) >= _BEST_COUNT:
            break
        if fb["id"] not in used:
            out.append(fb)
            used.add(fb["id"])
    return out[:_BEST_COUNT]


def get_news(
    page_size: int = _FETCH_PAGE_SIZE,
    api_key: str = "",
    base_url: str = "",
) -> tuple[list[dict], str]:
    """
    Fetch, score, and return the best 10 Guardian articles.

    Returns (articles, status) where status is one of:
      "live"     — fetched from API
      "fallback" — API unavailable, using fallback
      "demo"     — no API key, using fallback

    Never raises.
    """
    env_key, env_url = _load_env_key()
    api_key = api_key or env_key
    base_url = base_url or env_url

    if not api_key:
        return _fallback_articles(), "demo"

    try:
        raw = fetch_guardian_articles(api_key=api_key, base_url=base_url, page_size=page_size)
        normalized = [normalize_article(r) for r in raw]
        normalized = [a for a in normalized if a["title"] and a["href"]]
        # Hard exclusion: remove obituaries, death notices, violent drone/nuclear framing
        normalized = [
            a for a in normalized
            if not _is_disallowed_hud_headline(a["title"], a["tag"], a["summary"])
        ]
        normalized.sort(key=prometheus_relevance_score, reverse=True)
        best = normalized[:_BEST_COUNT]
        padded = pad_to_ten(best)
        return padded, "live"
    except Exception:
        return _fallback_articles(), "fallback"
