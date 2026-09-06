"""Fetch and extract article content from URLs using jina.ai Reader API."""

import httpx
import socket
import ipaddress
from urllib.parse import urlparse
from typing import Union
from pydantic import BaseModel
import re

FETCH_TIMEOUT = 15
JINA_API = "https://r.jina.ai/"


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
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
            resp = await client.get(JINA_API + url)

            if resp.status_code >= 400:
                return FetchError(error="http_error", detail=f"HTTP {resp.status_code}")

            markdown = resp.text.strip()
            if not markdown or len(markdown) < 50:
                return FetchError(error="extraction_failed", detail="Insufficient content")

    except httpx.TimeoutException:
        return FetchError(error="fetch_failed", detail="Request timeout")
    except httpx.ConnectError:
        return FetchError(error="fetch_failed", detail="Connection failed")
    except Exception as e:
        return FetchError(error="fetch_failed", detail=str(e)[:100])

    try:
        lines = markdown.split("\n")
        title = lines[0].lstrip("#").strip() if lines and lines[0].startswith("#") else "Untitled"

        body = "\n".join(lines[1:]) if len(lines) > 1 else markdown
        body = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", body)
        text = body.strip()

        if len(text) < 50:
            return FetchError(error="extraction_failed", detail="Insufficient article text")

        return Article(title=title, text=text)

    except Exception as e:
        return FetchError(error="extraction_failed", detail=str(e)[:100])
