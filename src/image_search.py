#!/usr/bin/env python3
"""Image sourcing: AI generation (primary), GIPHY / Unsplash (fallback), download, base64."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from src.common import image_api_key, image_api_url, image_cost_cny, image_model
from src.logger import get_logger

logger = get_logger(__name__)

GIPHY_API = "https://api.giphy.com/v1/gifs/search"
UNSPLASH_API = "https://api.unsplash.com/search/photos"

MAX_IMAGE_BYTES = 2 * 1024 * 1024  # 2 MB
TEMP_DIR = Path(tempfile.gettempdir()) / "x-reply-bot-images"


def _giphy_key() -> str:
    return (os.environ.get("GIPHY_API_KEY") or "").strip()


def _unsplash_key() -> str:
    return (os.environ.get("UNSPLASH_ACCESS_KEY") or "").strip()


def _ai_image_available() -> bool:
    return bool(image_api_key() and image_api_url())


def image_search_available() -> bool:
    return _ai_image_available() or bool(_giphy_key() or _unsplash_key())


def _image_gen_endpoint() -> str:
    base = image_api_url().rstrip("/")
    if not base.endswith("/v1"):
        base = base + "/v1"
    return f"{base}/images/generations"


def generate_ai_image(prompt: str) -> dict | None:
    """Generate an image via OpenAI-compatible image generation API.

    Returns a dict with 'url' or 'b64_json', 'source'='ai', or None on failure.
    Handles both standard OpenAI (url) and b64_json response formats.
    """
    if not _ai_image_available():
        return None
    model = image_model()
    endpoint = _image_gen_endpoint()
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": "1024x1024",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {image_api_key()}",
                "User-Agent": "x-reply-bot/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        logger.warning("ai image generation failed prompt=%s: %s", prompt, exc)
        return None

    images = (data.get("data") or []) if isinstance(data, dict) else []
    if not images:
        logger.warning("ai image generation returned no images prompt=%s", prompt)
        return None

    item = images[0]
    result: dict = {
        "source": "ai",
        "title": prompt,
        "width": 0,
        "height": 0,
        "cost_cny": image_cost_cny(),
    }

    b64 = item.get("b64_json", "")
    if b64:
        result["b64_json"] = b64
        return result

    image_url = item.get("url", "")
    if image_url:
        result["url"] = image_url
        return result

    return None


def search_giphy(query: str) -> dict | None:
    key = _giphy_key()
    if not key:
        return None
    url = f"{GIPHY_API}?api_key={key}&q={urllib.parse.quote(query)}&limit=1&rating=pg&lang=zh"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "x-reply-bot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        results = (data.get("data") or []) if isinstance(data, dict) else []
        if not results:
            return None
        gif = results[0]
        images = gif.get("images", {})
        downsized = images.get("downsized") or images.get("original") or {}
        image_url = downsized.get("url", "")
        if not image_url:
            return None
        return {
            "url": image_url,
            "source": "giphy",
            "title": gif.get("title", ""),
            "width": int(downsized.get("width") or 0),
            "height": int(downsized.get("height") or 0),
        }
    except Exception as exc:
        logger.warning("giphy search failed query=%s: %s", query, exc)
        return None


def search_unsplash(query: str) -> dict | None:
    key = _unsplash_key()
    if not key:
        return None
    url = f"{UNSPLASH_API}?query={urllib.parse.quote(query)}&per_page=1&orientation=squarish"
    try:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Client-ID {key}", "User-Agent": "x-reply-bot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        results = (data.get("results") or []) if isinstance(data, dict) else []
        if not results:
            return None
        photo = results[0]
        image_url = photo.get("urls", {}).get("small", "")
        if not image_url:
            return None
        return {
            "url": image_url,
            "source": "unsplash",
            "title": photo.get("description") or photo.get("alt_description") or "",
            "width": int(photo.get("width") or 0),
            "height": int(photo.get("height") or 0),
            "author": photo.get("user", {}).get("name", ""),
        }
    except Exception as exc:
        logger.warning("unsplash search failed query=%s: %s", query, exc)
        return None


def search_image(query: str) -> dict | None:
    if not query or not query.strip():
        return None
    q = query.strip()

    if _ai_image_available():
        result = generate_ai_image(q)
        if result:
            return result
        logger.info("ai generation failed, falling back to search")

    result = search_giphy(q)
    if not result:
        result = search_unsplash(q)
    return result


def download_image(url: str) -> tuple[str, str] | None:
    """Download image to a temp file. Returns (file_path, mime_type) or None."""
    # Reject non-HTTP(S) URLs strictly: urllib.request handles file://, ftp://,
    # etc., which would let a poisoned upstream response exfiltrate local files
    # or hit internal services. No scheme inference, no normalization.
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        logger.warning("download refused: unsupported url scheme url=%s", url)
        return None

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "x-reply-bot/1.0"})
        # 30s overall timeout is acceptable here because we also bound the
        # response body via Content-Length pre-check + bounded read below, so a
        # slow-loris peer cannot exhaust memory — only one socket for <=30s.
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_type = resp.headers.get("Content-Type", "")
            try:
                declared_len = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                declared_len = 0
            if declared_len > MAX_IMAGE_BYTES:
                logger.warning(
                    "image too large (Content-Length=%d) url=%s", declared_len, url
                )
                return None
            # Bounded read: pull at most MAX_IMAGE_BYTES + 1 bytes so we can
            # detect responses that omitted Content-Length but are still
            # oversized. Anything beyond the cap is treated as a rejection.
            data = resp.read(MAX_IMAGE_BYTES + 1)
    except Exception as exc:
        logger.warning("download failed url=%s: %s", url, exc)
        return None

    if len(data) > MAX_IMAGE_BYTES:
        logger.warning("image too large (truncated read): %d bytes", len(data))
        return None

    mime = content_type.split(";")[0].strip() or "image/gif"
    if not mime.startswith("image/"):
        logger.warning("download refused: non-image Content-Type=%s url=%s", mime, url)
        return None

    ext = mimetypes.guess_extension(mime) or ".gif"
    # sha1(url) is deterministic across processes (unlike hash(), which is
    # randomized via PYTHONHASHSEED) and collision-resistant for our purposes —
    # this avoids cross-process path divergence and intra-process races on
    # short hash collisions.
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    path = TEMP_DIR / f"img_{os.getpid()}_{digest}{ext}"
    path.write_bytes(data)
    return str(path), mime


def image_to_base64(path: str) -> tuple[str, str]:
    """Read image file, return (base64_data, mime_type)."""
    mime, _ = mimetypes.guess_type(path)
    mime = mime or "image/gif"
    data = Path(path).read_bytes()
    b64 = base64.b64encode(data).decode("ascii")
    return b64, mime


def cleanup_temp_image(path: str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass
