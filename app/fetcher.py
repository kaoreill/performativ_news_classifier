"""Retrieve and parse article content from a caller-supplied URL.

Retrieval is two-stage. A direct fetch is tried first: it is fast, adds no
third-party dependency, and succeeds for most publishers. When a publisher
blocks the direct fetch (bot detection) or serves a challenge page carrying no
real article text, retrieval falls back to a hosted extraction service
(jina.ai Reader). Both stages feed the same deterministic validation, so the
classifier always receives the same shape of input regardless of which path
produced it.

Neither stage can defeat publishers that hard-block automated clients outright
(Reuters and Investopedia return 401/402 to any non-browser caller, from any
IP). Those surface as a structured `http_error` rather than a silent failure.
"""

import ipaddress
import time
import os
import re
import socket
from typing import Optional, Union
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

# Stage timeouts stop any single stage hanging. They are tuning, not the
# contract: the caller passes a retrieval budget that both stages draw down
# from, so a slow direct fetch leaves the fallback correspondingly less time.
FETCH_TIMEOUT = 10
FALLBACK_TIMEOUT = 20  # the reader service renders pages, so it is slower
RETRIEVAL_BUDGET = 30.0

# Below this there is not enough time left for a stage to plausibly finish, so
# attempting it would only burn the remainder of the budget.
MIN_STAGE_SECONDS = 2.0

# Structured-data thresholds for the reader path. Both must be exceeded; see
# looks_like_machine_payload for why either alone would misfire on articles
# about data integration or financial infrastructure.
MACHINE_KV_PAIRS = 5
MACHINE_STRUCTURAL_SHARE = 0.10
MAX_RESPONSE_SIZE = 5 * 1024 * 1024  # 5MB: articles are never this big; DoS guard

# Below this, a 200 response is assumed to be a cookie wall or bot challenge
# rather than an article, and the fallback is attempted.
MIN_ARTICLE_CHARS = 500
# Absolute floor: below this after every strategy, extraction has failed.
MIN_ACCEPTABLE_CHARS = 200

MAX_TEXT_CHARS = 5000

HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")

# Interstitials that return HTTP 200 with enough text to look like a short
# article. Classifying one would produce a confident answer about a CAPTCHA page,
# so they are rejected as failed extractions instead.
CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "enable javascript and cookies",
    "checking your browser",
    "verify you are human",
    "access denied",
    "are you a robot",
    "unusual traffic",
    "captcha",
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class Article(BaseModel):
    title: str
    text: str
    source: str  # which retrieval strategy produced this: "direct" or "reader"


class FetchError(BaseModel):
    error: str
    detail: str


def is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local
    except ValueError:
        return False


def validate_url(url: str) -> Optional[FetchError]:
    """Scheme, hostname and SSRF checks. Returns None when the URL is safe."""
    try:
        parsed = urlparse(url)
    except Exception:
        return FetchError(error="invalid_url", detail="Could not parse URL")

    # No scheme at all is a malformed URL; a scheme we refuse to follow
    # (file:, ftp:, gopher:) is a deliberate block.
    if not parsed.scheme:
        return FetchError(error="invalid_url", detail="URL is missing a scheme")

    if parsed.scheme not in ("http", "https"):
        return FetchError(error="blocked_url", detail=f"Scheme '{parsed.scheme}' is not allowed; use http or https")

    if not parsed.netloc or not parsed.hostname:
        return FetchError(error="invalid_url", detail="URL is missing a hostname")

    try:
        for info in socket.getaddrinfo(parsed.hostname, None):
            if is_private_ip(info[4][0]):
                return FetchError(error="blocked_url", detail="Target resolves to a private/reserved IP")
    except socket.gaierror:
        return FetchError(error="fetch_failed", detail="DNS resolution failed")
    except Exception:
        return FetchError(error="fetch_failed", detail="Network error during host resolution")

    return None


def is_challenge_page(article: "Article") -> bool:
    """True when the retrieved page is a bot check rather than an article."""
    probe = f"{article.title} {article.text[:400]}".lower()
    return any(marker in probe for marker in CHALLENGE_MARKERS)


def looks_like_machine_payload(text: str) -> bool:
    """True when the retrieved text is structured data rather than prose.

    The direct path rejects non-HTML resources from the `Content-Type` header.
    The reader path cannot: it normalizes everything to text and does not report
    the source's content type, so a JSON API response arrives looking like an
    article. This inspects the payload's shape instead.

    Both signals must fire. Articles about enterprise data integration or
    regulated-workflow tooling — squarely inside the relevant themes — quote
    JSON, config and code, and would trip either signal alone. In measurements,
    article and index pages scored 0 key/value pairs and 3-5% structural
    punctuation, while a JSON API scored 97 and 13.2%; the thresholds sit far
    from the article range so this stays a backstop, not a quality judgement.
    """
    if not text:
        return False

    key_value_pairs = len(re.findall(r'"[A-Za-z_][A-Za-z0-9_]*"\s*:', text))
    structural_share = sum(text.count(c) for c in '{}[]":,') / len(text)

    return key_value_pairs >= MACHINE_KV_PAIRS and structural_share > MACHINE_STRUCTURAL_SHARE


def strip_html(html: str) -> str:
    """Reduce an HTML document to its visible text."""
    body_match = re.search(r"<body[^>]*>(.*?)</body>", html, re.IGNORECASE | re.DOTALL)
    body_html = body_match.group(1) if body_match else html

    # Drop chrome that would otherwise dominate the extracted text.
    body_html = re.sub(
        r"<(script|style|noscript|nav|header|footer|aside|form)[^>]*>.*?</\1>",
        " ",
        body_html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    text = re.sub(r"<[^>]+>", " ", body_html)
    text = re.sub(r"&nbsp;?", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_title(html: str) -> str:
    og = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if og:
        return og.group(1).strip()

    title = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if title:
        return title.group(1).strip()

    return "Untitled"


async def fetch_direct(url: str, timeout: float = FETCH_TIMEOUT) -> Union[Article, FetchError]:
    """Stage 1: fetch the page ourselves, size-capped and content-type checked."""
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers=BROWSER_HEADERS
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    return FetchError(error="http_error", detail=f"HTTP {resp.status_code}")

                content_type = resp.headers.get("content-type", "").lower()
                if content_type and not any(t in content_type for t in HTML_CONTENT_TYPES):
                    return FetchError(
                        error="unsupported_content_type",
                        detail=f"Cannot extract article text from {content_type.split(';')[0]}",
                    )

                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_RESPONSE_SIZE:
                        return FetchError(
                            error="fetch_failed",
                            detail=f"Response exceeds {MAX_RESPONSE_SIZE // (1024 * 1024)}MB limit",
                        )
                    chunks.append(chunk)

                encoding = resp.charset_encoding or "utf-8"
                html = b"".join(chunks).decode(encoding, errors="replace")

    except httpx.TimeoutException:
        return FetchError(error="fetch_failed", detail="Request timeout")
    except httpx.ConnectError:
        return FetchError(error="fetch_failed", detail="Connection failed")
    except Exception as e:
        return FetchError(error="fetch_failed", detail=str(e)[:100])

    return Article(title=extract_title(html), text=strip_html(html)[:MAX_TEXT_CHARS], source="direct")


async def fetch_via_reader(url: str, timeout: float = FALLBACK_TIMEOUT) -> Union[Article, FetchError]:
    """Stage 2: delegate retrieval and parsing to jina.ai Reader.

    An API key is optional but strongly recommended in deployment:
    unauthenticated requests are rate-limited per source IP, and a shared PaaS
    egress IP hits that limit quickly (this is what produced the 429s on Render).
    """
    # JSON mode rather than markdown: it returns the title as a field instead of
    # requiring us to regex it out of a "Title:" preamble, and it carries the
    # *source's* HTTP status, which the reader would otherwise hide behind its
    # own 200. That status is a deterministic gate; without it a reader-rendered
    # 404 page looks like a successful retrieval.
    headers = {"Accept": "application/json"}
    api_key = os.getenv("JINA_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(f"https://r.jina.ai/{url}", headers=headers)

            if resp.status_code == 429:
                return FetchError(error="fetch_failed", detail="Reader service rate limit reached")
            if resp.status_code >= 400:
                return FetchError(error="http_error", detail=f"Reader returned HTTP {resp.status_code}")

            payload = resp.json()

    except httpx.TimeoutException:
        return FetchError(error="fetch_failed", detail="Reader service timeout")
    except ValueError:
        return FetchError(error="extraction_failed", detail="Reader returned a malformed response")
    except Exception as e:
        return FetchError(error="fetch_failed", detail=str(e)[:100])

    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return FetchError(error="extraction_failed", detail="Reader returned no article data")

    # The reader answers 200 even when the origin refused; report the origin's
    # status so a rendered error page is not mistaken for an article.
    source_status = data.get("httpStatus")
    if isinstance(source_status, int) and source_status >= 400:
        return FetchError(error="http_error", detail=f"HTTP {source_status}")

    title = str(data.get("title") or "Untitled").strip() or "Untitled"
    text = re.sub(r"\s+", " ", str(data.get("content") or "")).strip()

    return Article(title=title, text=text[:MAX_TEXT_CHARS], source="reader")


async def fetch_and_extract(
    url: str, budget: float = RETRIEVAL_BUDGET
) -> Union[Article, FetchError]:
    """Retrieve and parse an article, falling back to a reader service when needed.

    `budget` is the total seconds retrieval may consume across both stages. The
    fallback gets whatever the direct attempt left, so a slow first stage cannot
    hand the second a full fresh timeout.
    """
    started = time.monotonic()

    blocked = validate_url(url)
    if blocked:
        return blocked

    direct = await fetch_direct(url, timeout=min(FETCH_TIMEOUT, max(budget, MIN_STAGE_SECONDS)))

    # A PDF or image will not become extractable via the fallback either.
    if isinstance(direct, FetchError) and direct.error == "unsupported_content_type":
        return direct

    # Neither retrieval path may hand structured data to the classifier. The
    # direct path normally catches this from the Content-Type header; this also
    # covers a server that mislabels JSON as text/html.
    if isinstance(direct, Article) and looks_like_machine_payload(direct.text):
        return FetchError(
            error="unsupported_content_type",
            detail="Retrieved payload is structured data, not article text",
        )

    def usable(candidate, min_chars: int) -> bool:
        return (
            isinstance(candidate, Article)
            and len(candidate.text) >= min_chars
            and not is_challenge_page(candidate)
        )

    if usable(direct, MIN_ARTICLE_CHARS):
        return direct

    # Direct fetch was blocked, errored, challenged, or returned too little text.
    remaining = budget - (time.monotonic() - started)
    if remaining >= MIN_STAGE_SECONDS:
        fallback = await fetch_via_reader(url, timeout=min(FALLBACK_TIMEOUT, remaining))

        # This is the case the header gate cannot reach: the direct fetch failed
        # before headers arrived, so the resource's type was never observed, and
        # the reader renders anything into text.
        if isinstance(fallback, Article) and looks_like_machine_payload(fallback.text):
            return FetchError(
                error="unsupported_content_type",
                detail="Retrieved payload is structured data, not article text",
            )

        if usable(fallback, MIN_ACCEPTABLE_CHARS):
            return fallback

    # Fallback failed too. Prefer thin-but-real direct content over nothing.
    if usable(direct, MIN_ACCEPTABLE_CHARS):
        return direct

    # Report the original blocking status where there was one; a bot challenge
    # that returned 200 is an extraction failure, not a transport failure.
    if isinstance(direct, FetchError):
        return direct

    return FetchError(
        error="extraction_failed",
        detail="Could not identify meaningful article text (paywall or bot challenge)",
    )
