"""Fetch and extract article content from URLs."""

import httpx
import socket
import ipaddress
import re
from urllib.parse import urlparse
from typing import Union
from pydantic import BaseModel

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
    """Fetch article from URL using jina.ai Reader API."""
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
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url)

            if resp.status_code >= 400:
                return FetchError(error="http_error", detail=f"HTTP {resp.status_code}")

            html = resp.text

    except httpx.TimeoutException:
        return FetchError(error="fetch_failed", detail="Request timeout")
    except httpx.ConnectError:
        return FetchError(error="fetch_failed", detail="Connection failed")
    except Exception as e:
        return FetchError(error="fetch_failed", detail=str(e)[:100])

    try:
        title_match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
        title = title_match.group(1).strip() if title_match else "Untitled"

        og_title = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
        if og_title:
            title = og_title.group(1).strip()

        body_match = re.search(r"<body[^>]*>(.*?)</body>", html, re.IGNORECASE | re.DOTALL)
        body_html = body_match.group(1) if body_match else html

        text = re.sub(r"<[^>]+>", " ", body_html)
        text = re.sub(r"\s+", " ", text).strip()
        text = text[:5000]

        if not text or len(text) < 50:
            return FetchError(error="extraction_failed", detail="Insufficient content")

        return Article(title=title, text=text)

    except Exception as e:
        return FetchError(error="extraction_failed", detail=str(e)[:100])
