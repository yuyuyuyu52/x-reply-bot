"""Sanity checks for src.image_search.

Pins down security-sensitive download regressions and the GIPHY/Unsplash
JSON shape handling. All network is mocked via monkeypatch on
`urllib.request.urlopen` — no test is allowed to make a real request.

Locked-down regressions:
  - URL scheme allowlist (only http(s); reject file://, ftp://, javascript:,
    empty, None) so a poisoned upstream response can't exfiltrate local files
    or hit internal services via urllib's transport switchboard.
  - Content-Length precheck — refuse before reading body if the server
    declares > MAX_IMAGE_BYTES.
  - Bounded read — read at most MAX_IMAGE_BYTES + 1 even when Content-Length
    is missing, so a slow-loris peer can't exhaust memory.
  - MIME validation — refuse non-image/* Content-Type.
  - sha1(url) filename — deterministic across processes; not Python's hash().
  - GIPHY/Unsplash: missing API key → None; empty results → None; happy path
    returns a usable url string.
  - image_to_base64 round-trips bytes correctly.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import src.image_search as image_search  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _FakeResp:
    """Mimics the subset of urllib response API that image_search uses."""

    def __init__(self, body: bytes = b"", headers=None, raise_on_read: bool = False):
        self._body = body
        self.headers = headers or {}
        self._read_calls: list[tuple] = []
        self._raise_on_read = raise_on_read

    def read(self, *args, **kwargs):
        self._read_calls.append((args, kwargs))
        if self._raise_on_read:
            raise AssertionError("read() should not be called")
        if args:
            return self._body[: args[0]]
        return self._body

    # context-manager protocol
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _MockHeaders(dict):
    """dict-like that mimics http.client.HTTPMessage's .get()."""

    def get(self, key, default=None):
        # Header keys are case-insensitive in real responses.
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


@pytest.fixture(autouse=True)
def _redirect_temp_dir(tmp_path, monkeypatch):
    """Redirect the module-level TEMP_DIR so test writes don't leak to /tmp."""
    target = tmp_path / "imgs"
    monkeypatch.setattr(image_search, "TEMP_DIR", target, raising=True)
    yield target


# ---------------------------------------------------------------------------
# URL scheme allowlist regression
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x.gif",
        "javascript://alert(1)",
        "data:image/gif;base64,AAAA",
        "",
        None,
        123,  # non-string
    ],
)
def test_download_image_rejects_non_http_urls_without_calling_urlopen(bad_url, monkeypatch):
    """Regression: any non-http(s) URL must short-circuit before urlopen runs."""
    urlopen = MagicMock(side_effect=AssertionError("urlopen must not be called"))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    # image_search references urllib.request.urlopen via attribute lookup, so the
    # monkeypatch above is enough — but to be safe, patch the module's own
    # urllib.request as well.
    monkeypatch.setattr(image_search.urllib.request, "urlopen", urlopen)

    assert image_search.download_image(bad_url) is None
    urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# Content-Length precheck regression
# ---------------------------------------------------------------------------


def test_download_image_rejects_oversized_content_length_without_reading_body(monkeypatch):
    """Regression: if Content-Length > MAX_IMAGE_BYTES, refuse before reading."""
    headers = _MockHeaders({
        "Content-Type": "image/gif",
        "Content-Length": str(image_search.MAX_IMAGE_BYTES + 10_000_000),
    })
    fake = _FakeResp(body=b"never-read", headers=headers, raise_on_read=True)

    urlopen = MagicMock(return_value=fake)
    monkeypatch.setattr(image_search.urllib.request, "urlopen", urlopen)

    assert image_search.download_image("https://example.com/big.gif") is None
    # urlopen happened, but body was never consumed.
    urlopen.assert_called_once()
    assert fake._read_calls == [], fake._read_calls


# ---------------------------------------------------------------------------
# Bounded read regression (missing Content-Length)
# ---------------------------------------------------------------------------


def test_download_image_uses_bounded_read_when_content_length_missing(monkeypatch):
    """Regression: resp.read must be called with `MAX_IMAGE_BYTES + 1`, not
    unbounded, so a peer that omits Content-Length cannot exhaust memory."""
    headers = _MockHeaders({"Content-Type": "image/gif"})  # no Content-Length
    body = b"GIF89a" + b"\x00" * 128
    fake = _FakeResp(body=body, headers=headers)

    urlopen = MagicMock(return_value=fake)
    monkeypatch.setattr(image_search.urllib.request, "urlopen", urlopen)

    result = image_search.download_image("https://example.com/x.gif")
    assert result is not None
    assert fake._read_calls, "read() was never called"
    # First positional arg to read() must be the bounded cap.
    args, kwargs = fake._read_calls[0]
    assert args and args[0] == image_search.MAX_IMAGE_BYTES + 1, fake._read_calls


def test_download_image_rejects_when_bounded_read_returns_oversize(monkeypatch):
    """If the bounded read returns more than MAX_IMAGE_BYTES bytes, reject."""
    headers = _MockHeaders({"Content-Type": "image/gif"})
    # MAX_IMAGE_BYTES + 1 bytes total — exactly the trigger size.
    body = b"\x00" * (image_search.MAX_IMAGE_BYTES + 1)
    fake = _FakeResp(body=body, headers=headers)
    monkeypatch.setattr(image_search.urllib.request, "urlopen", MagicMock(return_value=fake))

    assert image_search.download_image("https://example.com/x.gif") is None


# ---------------------------------------------------------------------------
# MIME validation regression
# ---------------------------------------------------------------------------


def test_download_image_refuses_non_image_content_type(monkeypatch, tmp_path):
    headers = _MockHeaders({"Content-Type": "text/html; charset=utf-8"})
    fake = _FakeResp(body=b"<html>nope</html>", headers=headers)
    monkeypatch.setattr(image_search.urllib.request, "urlopen", MagicMock(return_value=fake))

    target = tmp_path / "imgs"
    # _redirect_temp_dir already retargeted TEMP_DIR; assert no file slipped in.
    assert image_search.download_image("https://example.com/x") is None
    if target.exists():
        assert list(target.iterdir()) == []


# ---------------------------------------------------------------------------
# sha1(url) filename regression
# ---------------------------------------------------------------------------


def test_download_image_filename_uses_sha1_of_url_not_python_hash(monkeypatch):
    """Regression: sha1 is deterministic across processes; Python's hash() is
    randomized via PYTHONHASHSEED and would diverge between processes."""
    url = "https://example.com/cute.gif"
    headers = _MockHeaders({"Content-Type": "image/gif"})
    body = b"GIF89a-mini"
    fake = _FakeResp(body=body, headers=headers)
    monkeypatch.setattr(image_search.urllib.request, "urlopen", MagicMock(return_value=fake))

    result = image_search.download_image(url)
    assert result is not None
    path_str, mime = result
    assert mime == "image/gif"
    expected_digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    stem = Path(path_str).stem
    # Filename layout is f"img_{pid}_{digest}{ext}".
    assert stem.endswith(expected_digest), (stem, expected_digest)
    # And definitely not Python's randomized hash().
    assert str(hash(url)) not in stem


def test_download_image_writes_file_contents_match_body(monkeypatch):
    body = b"GIF89a-mini-payload"
    fake = _FakeResp(body=body, headers=_MockHeaders({"Content-Type": "image/gif"}))
    monkeypatch.setattr(image_search.urllib.request, "urlopen", MagicMock(return_value=fake))

    result = image_search.download_image("https://example.com/y.gif")
    assert result is not None
    path_str, _ = result
    assert Path(path_str).read_bytes() == body


# ---------------------------------------------------------------------------
# GIPHY
# ---------------------------------------------------------------------------


def test_search_giphy_missing_key_returns_none(monkeypatch):
    monkeypatch.delenv("GIPHY_API_KEY", raising=False)
    # Even if urlopen were callable, the function must short-circuit.
    urlopen = MagicMock(side_effect=AssertionError("must not call HTTP"))
    monkeypatch.setattr(image_search.urllib.request, "urlopen", urlopen)
    assert image_search.search_giphy("cats") is None
    urlopen.assert_not_called()


def test_search_giphy_happy_path(monkeypatch):
    monkeypatch.setenv("GIPHY_API_KEY", "fake-key")
    payload = {
        "data": [
            {
                "title": "Cat GIF",
                "images": {
                    "downsized": {
                        "url": "https://giphy.example/abc.gif",
                        "width": "320",
                        "height": "240",
                    }
                },
            }
        ]
    }
    import json as _json
    fake = _FakeResp(body=_json.dumps(payload).encode("utf-8"), headers=_MockHeaders())
    monkeypatch.setattr(image_search.urllib.request, "urlopen", MagicMock(return_value=fake))

    result = image_search.search_giphy("cats")
    assert result is not None
    assert result["url"] == "https://giphy.example/abc.gif"
    assert result["source"] == "giphy"
    assert result["width"] == 320 and result["height"] == 240


def test_search_giphy_empty_results_returns_none(monkeypatch):
    monkeypatch.setenv("GIPHY_API_KEY", "fake-key")
    import json as _json
    fake = _FakeResp(body=_json.dumps({"data": []}).encode("utf-8"), headers=_MockHeaders())
    monkeypatch.setattr(image_search.urllib.request, "urlopen", MagicMock(return_value=fake))
    assert image_search.search_giphy("nothing") is None


# ---------------------------------------------------------------------------
# Unsplash
# ---------------------------------------------------------------------------


def test_search_unsplash_missing_key_returns_none(monkeypatch):
    monkeypatch.delenv("UNSPLASH_ACCESS_KEY", raising=False)
    urlopen = MagicMock(side_effect=AssertionError("must not call HTTP"))
    monkeypatch.setattr(image_search.urllib.request, "urlopen", urlopen)
    assert image_search.search_unsplash("trees") is None
    urlopen.assert_not_called()


def test_search_unsplash_happy_path(monkeypatch):
    monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "fake-key")
    payload = {
        "results": [
            {
                "urls": {"small": "https://unsplash.example/img.jpg"},
                "description": "a tree",
                "width": 400,
                "height": 400,
                "user": {"name": "Photog"},
            }
        ]
    }
    import json as _json
    fake = _FakeResp(body=_json.dumps(payload).encode("utf-8"), headers=_MockHeaders())
    monkeypatch.setattr(image_search.urllib.request, "urlopen", MagicMock(return_value=fake))

    result = image_search.search_unsplash("trees")
    assert result is not None
    assert result["url"] == "https://unsplash.example/img.jpg"
    assert result["source"] == "unsplash"
    assert result["author"] == "Photog"


def test_search_unsplash_empty_results_returns_none(monkeypatch):
    monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "fake-key")
    import json as _json
    fake = _FakeResp(body=_json.dumps({"results": []}).encode("utf-8"), headers=_MockHeaders())
    monkeypatch.setattr(image_search.urllib.request, "urlopen", MagicMock(return_value=fake))
    assert image_search.search_unsplash("void") is None


# ---------------------------------------------------------------------------
# search_image dispatch (AI off → giphy → unsplash)
# ---------------------------------------------------------------------------


def test_search_image_empty_query_returns_none(monkeypatch):
    monkeypatch.delenv("X_REPLY_IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("X_REPLY_IMAGE_API_URL", raising=False)
    assert image_search.search_image("") is None
    assert image_search.search_image("   ") is None


def test_search_image_falls_back_to_unsplash_when_giphy_empty(monkeypatch):
    """With no AI key and no GIPHY key, only Unsplash should be hit."""
    monkeypatch.delenv("X_REPLY_IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("X_REPLY_IMAGE_API_URL", raising=False)
    monkeypatch.delenv("GIPHY_API_KEY", raising=False)
    monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "u-key")

    import json as _json
    payload = {
        "results": [
            {"urls": {"small": "https://unsplash.example/fallback.jpg"}, "user": {}},
        ]
    }
    fake = _FakeResp(body=_json.dumps(payload).encode("utf-8"), headers=_MockHeaders())
    monkeypatch.setattr(image_search.urllib.request, "urlopen", MagicMock(return_value=fake))

    result = image_search.search_image("kittens")
    assert result is not None
    assert result["source"] == "unsplash"
    assert result["url"] == "https://unsplash.example/fallback.jpg"


# ---------------------------------------------------------------------------
# image_to_base64 round trip
# ---------------------------------------------------------------------------


def test_image_to_base64_roundtrip(tmp_path):
    raw = b"\x89PNG\r\n\x1a\nfake-png-bytes\x00\x01\x02"
    p = tmp_path / "img.png"
    p.write_bytes(raw)

    b64, mime = image_search.image_to_base64(str(p))
    assert mime == "image/png"
    assert base64.b64decode(b64) == raw


def test_image_to_base64_unknown_extension_defaults_to_gif(tmp_path, monkeypatch):
    """When mimetypes can't guess the type (returns None), fall back to image/gif."""
    p = tmp_path / "blob.unknownext"
    p.write_bytes(b"unknown")
    # Force the (None, None) branch by stubbing guess_type — depending on the
    # host's mime registry, some "unknown" extensions still resolve to
    # application/octet-stream, which would skip the fallback.
    monkeypatch.setattr(image_search.mimetypes, "guess_type", lambda *a, **kw: (None, None))
    _, mime = image_search.image_to_base64(str(p))
    assert mime == "image/gif"


if __name__ == "__main__":
    unittest.main()
