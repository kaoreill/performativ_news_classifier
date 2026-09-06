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
import os
import re
import socket
from typing import Optional, Union
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

FETCH_TIMEOUT = 15
FALLBACK_TIMEOUT = 25  # the reader service renders pages, so it is slower
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


async def fetch_direct(url: str) -> Union[Article, FetchError]:
    """Stage 1: fetch the page ourselves, size-capped and content-type checked."""
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT, follow_redirects=True, headers=BROWSER_HEADERS
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


async def fetch_via_reader(url: str) -> Union[Article, FetchError]:
    """Stage 2: delegate retrieval and parsing to jina.ai Reader.

    An API key is optional but strongly recommended in deployment:
    unauthenticated requests are rate-limited per source IP, and a shared PaaS
    egress IP hits that limit quickly (this is what produced the 429s on Render).
    """
    headers = {"Accept": "text/plain"}
    api_key = os.getenv("JINA_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=FALLBACK_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(f"https://r.jina.ai/{url}", headers=headers)

            if resp.status_code == 429:
                return FetchError(error="fetch_failed", detail="Reader service rate limit reached")
            if resp.status_code >= 400:
                return FetchError(error="http_error", detail=f"Reader returned HTTP {resp.status_code}")

            content = resp.text

    except httpx.TimeoutException:
        return FetchError(error="fetch_failed", detail="Reader service timeout")
    except Exception as e:
        return FetchError(error="fetch_failed", detail=str(e)[:100])

    # Reader output is markdown prefixed with "Title:" / "URL Source:" metadata lines.
    title = "Untitled"
    title_match = re.search(r"^Title:\s*(.+)$", content, re.MULTILINE)
    if title_match:
        title = title_match.group(1).strip()

    body = content
    marker = re.search(r"^Markdown Content:\s*$", content, re.MULTILINE)
    if marker:
        body = content[marker.end():]

    text = re.sub(r"\s+", " ", body).strip()
    return Article(title=title, text=text[:MAX_TEXT_CHARS], source="reader")


async def fetch_and_extract(url: str) -> Union[Article, FetchError]:
    """Retrieve and parse an article, falling back to a reader service when needed."""
    blocked = validate_url(url)
    if blocked:
        return blocked

    direct = await fetch_direct(url)

    # A PDF or image will not become extractable via the fallback either.
    if isinstance(direct, FetchError) and direct.error == "unsupported_content_type":
        return direct

    def usable(candidate, min_chars: int) -> bool:
        return (
            isinstance(candidate, Article)
            and len(candidate.text) >= min_chars
            and not is_challenge_page(candidate)
        )

    if usable(direct, MIN_ARTICLE_CHARS):
        return direct

    # Direct fetch was blocked, errored, challenged, or returned too little text.
    fallback = await fetch_via_reader(url)
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
