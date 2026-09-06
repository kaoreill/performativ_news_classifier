"""Fetch and extract article content from URLs using Mercury Parser API."""

import httpx
import socket
import ipaddress
import os
from urllib.parse import urlparse
from typing import Union
from pydantic import BaseModel

FETCH_TIMEOUT = 15
PARSER_API = "https://api.mercury.postlight.com/parser"
PARSER_KEY = os.getenv("MERCURY_API_KEY", "")


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
            params = {"url": url}
            if PARSER_KEY:
                params["api_key"] = PARSER_KEY
            resp = await client.get(PARSER_API, params=params)

            if resp.status_code >= 400:
                return FetchError(error="http_error", detail=f"HTTP {resp.status_code}")

            data = resp.json()
            if data.get("error"):
                return FetchError(error="extraction_failed", detail=data.get("error", "Parse failed"))

            title = data.get("title", "Untitled").strip()
            text = data.get("content", "").strip()

            if not text or len(text) < 50:
                return FetchError(error="extraction_failed", detail="Insufficient content")

            return Article(title=title, text=text)

    except httpx.TimeoutException:
        return FetchError(error="fetch_failed", detail="Request timeout")
    except httpx.ConnectError:
        return FetchError(error="fetch_failed", detail="Connection failed")
    except ValueError:
        return FetchError(error="extraction_failed", detail="Invalid JSON response")
    except Exception as e:
        return FetchError(error="fetch_failed", detail=str(e)[:100])
