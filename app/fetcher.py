"""Fetch and extract article content from URLs."""

import httpx
import socket
import ipaddress
from urllib.parse import urlparse
from typing import Union
import trafilatura
from pydantic import BaseModel

MAX_RESPONSE_SIZE = 5 * 1024 * 1024
FETCH_TIMEOUT = 15


class Article(BaseModel):
    title: str
    text: str


class FetchError(BaseModel):
    error: str
    detail: str


def is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local
    except ValueError:
        return False


async def fetch_and_extract(url: str) -> Union[Article, FetchError]:
    """Fetch article from URL and extract text."""
    try:
        parsed = urlparse(url)
    except Exception:
        return FetchError(error="invalid_url", detail="Could not parse URL")

    if parsed.scheme not in ("http", "https"):
        return FetchError(error="blocked_url", detail="Only http/https schemes allowed")

    if not parsed.netloc or not parsed.hostname:
        return FetchError(error="invalid_url", detail="Invalid hostname")

    try:
        ip = socket.gethostbyname(parsed.hostname)
        if is_private_ip(ip):
            return FetchError(error="blocked_url", detail="Target is private/reserved IP")
    except socket.gaierror:
        return FetchError(error="fetch_failed", detail="DNS resolution failed")
    except Exception:
        return FetchError(error="fetch_failed", detail="Network error")

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url)

            if resp.status_code >= 400:
                return FetchError(error="http_error", detail=f"HTTP {resp.status_code}")

            content_type = resp.headers.get("content-type", "").lower()
            if not any(ct in content_type for ct in ["text/html", "application/xhtml"]):
                return FetchError(error="unsupported_content_type", detail=f"Content-Type: {content_type}")

            if len(resp.content) > MAX_RESPONSE_SIZE:
                return FetchError(error="fetch_failed", detail="Response too large (>5MB)")

    except httpx.TimeoutException:
        return FetchError(error="fetch_failed", detail="Request timeout")
    except httpx.ConnectError:
        return FetchError(error="fetch_failed", detail="Connection failed")
    except Exception as e:
        return FetchError(error="fetch_failed", detail=str(e)[:100])

    try:
        extracted = trafilatura.extract(resp.text, include_comments=False, output_format="python")
        if not extracted:
            return FetchError(error="extraction_failed", detail="Could not extract article text")

        title = extracted.get("title") or "Untitled"
        text = extracted.get("raw_text") or extracted.get("text") or ""

        if not text or len(text.strip()) < 50:
            return FetchError(error="extraction_failed", detail="Insufficient article text")

        return Article(title=title, text=text)

    except Exception:
        return FetchError(error="extraction_failed", detail="Extraction failed")
