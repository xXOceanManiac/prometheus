"""
prometheus/services/guardian_news.py — Guardian API news service.

Fetches articles from The Guardian, applies Prometheus-specific relevance scoring,
selects the best 9, and pads with fallback data when the API is unavailable.

Config (read from environment at call time — no module-level side effects):
  VITE_GUARDIAN_API_KEY    — Guardian API key
  VITE_GUARDIAN_API_URL    — base URL (default: https://content.guardianapis.com/search)

Usage:
  from prometheus.services.guardian_news import get_news
  articles = get_news()          # returns list of up to 9 normalized article dicts
  articles = get_news(live=True) # force a live fetch; raise on failure
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional

_DEFAULT_URL = "https://content.guardianapis.com/search"
_FETCH_PAGE_SIZE = 50
_BEST_COUNT = 9


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
    # Technology / AI / Automation
    "artificial intelligence", "ai agent", "llm", "openai", "claude", "gemini", "language model",
    "automation", "software", "coding", "developer", "open source", "algorithm", "machine learning",
    "data center", "semiconductor", "chips", "robotics", "computing",
    # Business / Markets
    "startup", "venture capital", "private equity", "ipo", "merger", "acquisition", "earnings",
    "inflation", "interest rate", "gdp", "economy", "market", "stock", "investment", "revenue",
    "supply chain", "trade", "tariff", "unemployment", "recession", "growth",
    # Education / Microschools
    "microschool", "school choice", "homeschool", "charter school", "edtech", "education",
    "learning", "curriculum", "student", "tuition",
    # Science / Engineering
    "space", "nasa", "physics", "quantum", "nuclear", "energy", "climate", "engineering",
    "biology", "medicine", "research", "breakthrough",
    # Florida / Local
    "florida", "south florida", "miami", "orlando", "tallahassee", "hurricane", "everglades",
    # Faith / Culture / Family
    "faith", "church", "family", "parenting", "culture", "tradition", "community", "character",
    # Health / Fitness
    "fitness", "nutrition", "gym", "spartan", "health", "training", "longevity", "sleep",
    # Geopolitics
    "geopolitics", "conflict", "war", "nato", "china", "russia", "middle east", "election",
    "president", "congress", "supreme court", "regulation", "policy",
    # Media / Entertainment
    "gaming", "xbox", "spotify", "film", "music", "design", "architecture", "creative",
]

_MEDIUM_SIGNAL = [
    "company", "ceo", "fund", "infrastructure", "logistics", "pricing", "demand",
    "manufacturing", "exports", "imports", "housing", "federal", "senate", "governor",
    "university", "professor", "study", "analysis", "report", "trend",
]

_PENALTY = [
    # Celebrity / clickbait
    "celebrity", "kardashian", "influencer", "dating rumor", "red carpet", "award show",
    # Low-signal crime / sports
    "arrested for", "convicted of", "scored against", "match result", "game score",
    # UK-only narrow politics
    "parliament uk", "labour party", "conservative party", "tory", "westminster",
]


def prometheus_relevance_score(article: dict) -> float:
    """
    Score an article for relevance to Tate's interests.

    High: AI/tech, business/markets, education, science, Florida/local, faith/culture,
          health/fitness, geopolitics, gaming/entertainment, design.
    Medium: general business indicators, policy, academia.
    Penalty: celebrity gossip, low-signal crime/sports scores, narrow UK politics.
    Boost: recency, analysis/explainer.
    """
    hay = f"{article.get('title','')} {article.get('summary','')} {article.get('tag','')}".lower()

    score = 0.0
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
    tag = (article.get("tag") or "").lower()
    if tag in ("technology", "business", "science", "education", "us-news"):
        score += 3.0
    elif tag in ("world", "environment", "politics", "media", "sport"):
        score += 1.0

    # Analysis/explainer boost — tends to be higher signal
    title_lower = (article.get("title") or "").lower()
    if any(kw in title_lower for kw in ("explained", "analysis", "what is", "why ", "how ")):
        score += 3.0

    return score


# ── Fallback data ─────────────────────────────────────────────────────────────

def _fallback_articles() -> list[dict]:
    """9 Prometheus-specific fallback articles shown when API is unavailable."""
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


def pad_to_nine(articles: list[dict]) -> list[dict]:
    """Ensure exactly 9 articles by padding with fallback items."""
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
    Fetch, score, and return the best 9 Guardian articles.

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
        normalized.sort(key=prometheus_relevance_score, reverse=True)
        best = normalized[:_BEST_COUNT]
        padded = pad_to_nine(best)
        return padded, "live"
    except Exception:
        return _fallback_articles(), "fallback"
