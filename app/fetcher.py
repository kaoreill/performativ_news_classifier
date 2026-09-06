"""Retrieve and parse article content from a caller-supplied URL.

Retrieval is two-stage. A direct fetch is tried first: it is fast, adds no
third-party dependency, and succeeds for most publishers. When a publisher
blocks the direct fetch (bot detection), serves a challenge page carrying no
real article text, or returns implausibly little of it, retrieval falls back to
a hosted extraction service (jina.ai Reader). Both stages feed the same
deterministic validation, so the classifier always receives the same shape of
input regardless of which path produced it.

Some URLs are retrievable by neither stage. Which ones is not a fixed property
— anti-bot posture is the publisher's to change at any time — so no publisher
is named here as blocked. What is fixed is the contract: a URL we cannot read
surfaces as a structured error naming where the pipeline stopped, never as a
silent failure or an invented article.
"""

import asyncio
import ipaddress
import os
import re
import socket
import time
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

# Redirects are followed by hand so that each hop can be revalidated, which
# means the chain also needs a bound of its own. Five is well above what real
# publishers use (http -> https -> www -> canonical) and well below anything
# worth calling a loop.
MAX_REDIRECTS = 5

# Below this, a 200 response is assumed to be a cookie wall or bot challenge
# rather than an article, and the fallback is attempted.
MIN_ARTICLE_CHARS = 500
# Absolute floor: below this after every strategy, extraction has failed.
MIN_ACCEPTABLE_CHARS = 200

# Retained article text. The classifier reads only the first 2000 characters
# (classifier.MAX_INPUT_CHARS); the wider slice kept here is deliberate, not an
# oversight. looks_like_machine_payload scores the whole retained string, so
# narrowing this to the classifier's window would shrink the evidence that gate
# runs on, and the retrieved length is reported as a diagnostic.
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


async def validate_url(url: str) -> FetchError | None:
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

    # Resolved on the loop's executor rather than inline: getaddrinfo blocks,
    # and every SSRF check would otherwise stall each other in-flight request
    # for the duration of a lookup.
    try:
        resolved = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        return FetchError(error="fetch_failed", detail="DNS resolution failed")
    except Exception:
        return FetchError(error="fetch_failed", detail="Network error during host resolution")

    for info in resolved:
        if is_private_ip(info[4][0]):
            return FetchError(error="blocked_url", detail="Target resolves to a private/reserved IP")

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


def reject_unsafe_peer(response: httpx.Response) -> FetchError | None:
    """Check the address actually connected to, not the one resolved beforehand.

    validate_url resolves the hostname, and the client resolves it again when it
    opens the connection. Those are two separate lookups, so a DNS server under
    an attacker's control can answer them differently: a public address for the
    check, a private one for the connection that follows. Reading the peer
    address back off the open socket closes that window, because it inspects the
    connection that was made rather than predicting which one will be made.

    Returns None when the transport exposes no peer address. That fails open
    deliberately — validate_url still ran, so the guarantee is exactly the one
    held before this function existed, whereas failing closed would take the
    whole service down on a transport change.
    """
    stream = response.extensions.get("network_stream")
    if stream is None:
        return None

    address = stream.get_extra_info("server_addr")
    if not isinstance(address, tuple) or not address:
        return None

    if is_private_ip(str(address[0])):
        return FetchError(
            error="blocked_url",
            detail="Connection resolved to a private/reserved IP",
        )
    return None


def reject_if_machine_payload(candidate: Article | FetchError) -> FetchError | None:
    """Enforce that neither retrieval path may hand structured data to the classifier.

    Both stages route through this one function, so the guarantee is a property
    of the pipeline rather than of whichever path happened to run — which is
    what makes it testable as an invariant instead of per-URL behaviour.
    Anything that is not a machine payload returns None, a FetchError included,
    leaving the caller's own error handling untouched.
    """
    if isinstance(candidate, Article) and looks_like_machine_payload(candidate.text):
        return FetchError(
            error="unsupported_content_type",
            detail="Retrieved payload is structured data, not article text",
        )
    return None


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


async def fetch_direct(url: str, timeout: float = FETCH_TIMEOUT) -> Article | FetchError:
    """Stage 1: fetch the page ourselves, size-capped and content-type checked.

    Redirects are followed here rather than by httpx so that every hop clears the
    same checks as the URL the caller supplied. Automatic redirect following
    validates the first URL and nothing after it, which lets an allowed page hand
    the request to a private address simply by answering with a Location header.
    """
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, headers=BROWSER_HEADERS
        ) as client:
            target = url
            for _ in range(MAX_REDIRECTS + 1):
                async with client.stream("GET", target) as resp:
                    unsafe = reject_unsafe_peer(resp)
                    if unsafe:
                        return unsafe

                    if resp.is_redirect:
                        # is_redirect is only true when a Location header is present.
                        target = str(resp.url.join(resp.headers["location"]))
                        blocked = await validate_url(target)
                        if blocked:
                            return blocked
                        continue

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
                    break
            else:
                return FetchError(
                    error="fetch_failed", detail=f"Exceeded {MAX_REDIRECTS} redirects"
                )

    except httpx.TimeoutException:
        return FetchError(error="fetch_failed", detail="Request timeout")
    except httpx.ConnectError:
        return FetchError(error="fetch_failed", detail="Connection failed")
    except Exception as e:
        return FetchError(error="fetch_failed", detail=str(e)[:100])

    return Article(title=extract_title(html), text=strip_html(html)[:MAX_TEXT_CHARS], source="direct")


async def fetch_via_reader(url: str, timeout: float = FALLBACK_TIMEOUT) -> Article | FetchError:
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

            # validate_url vets the article URL, which the reader fetches from
            # its own infrastructure. This connection is to the reader itself,
            # so it gets the same peer check rather than none at all.
            unsafe = reject_unsafe_peer(resp)
            if unsafe:
                return unsafe

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
) -> Article | FetchError:
    """Retrieve and parse an article, falling back to a reader service when needed.

    `budget` is the total seconds retrieval may consume across both stages. The
    fallback gets whatever the direct attempt left, so a slow first stage cannot
    hand the second a full fresh timeout.
    """
    started = time.monotonic()

    blocked = await validate_url(url)
    if blocked:
        return blocked

    direct = await fetch_direct(url, timeout=min(FETCH_TIMEOUT, max(budget, MIN_STAGE_SECONDS)))

    # A PDF or image will not become extractable via the fallback either.
    if isinstance(direct, FetchError) and direct.error == "unsupported_content_type":
        return direct

    # The direct path normally catches structured data from the Content-Type
    # header; this also covers a server that mislabels JSON as text/html.
    rejected = reject_if_machine_payload(direct)
    if rejected:
        return rejected

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
        rejected = reject_if_machine_payload(fallback)
        if rejected:
            return rejected

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
