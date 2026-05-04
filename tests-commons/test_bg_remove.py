"""Integration tests for /commons/image/remove-bg — run against a live
surface-api at 127.0.0.1:3000 with a service-key + an existing seeded agent.

These tests use a real fixture (synthetic portrait) and the real rembg/U2-Net
model — no mocks. First invocation triggers a ~170 MB model download to
~/.u2net/, so the timeout is generous.
"""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import httpx
import pytest


FIX = Path(__file__).parent / "fixtures"
BASE = os.environ.get("SURFACE_BASE_URL", "http://127.0.0.1:3000")
SVC_KEY = os.environ.get("SURFACE_SERVICE_KEY") or os.environ.get("SERVICE_API_KEY")
AGENT_ID = os.environ.get("SURFACE_TEST_AGENT_ID")


def _client() -> httpx.Client:
    if not SVC_KEY or not AGENT_ID:
        pytest.skip("SURFACE_SERVICE_KEY + SURFACE_TEST_AGENT_ID required")
    return httpx.Client(
        base_url=BASE,
        headers={"X-Service-Key": SVC_KEY, "X-Agent-Id": AGENT_ID},
        timeout=300.0,
    )


def _alpha_stats(png_bytes: bytes) -> tuple[int, int, int, int]:
    """Return (width, height, total_px, non_transparent_px)."""
    from PIL import Image
    im = Image.open(io.BytesIO(png_bytes))
    assert im.mode == "RGBA", f"expected RGBA, got {im.mode}"
    alpha = im.split()[-1]
    total = im.width * im.height
    non_t = sum(1 for p in alpha.getdata() if p > 0)
    return im.width, im.height, total, non_t


def test_health_public():
    r = httpx.get(f"{BASE}/commons/image/health", timeout=10)
    assert r.status_code == 200
    j = r.json()
    assert j["service"] == "commons.image"
    assert "available" in j
    assert "sc_cost" in j
    assert j["sc_cost"]["remove_bg"] == 2.0


def test_remove_bg_single():
    with _client() as c:
        files = [("file", ("portrait.jpg", (FIX / "portrait.jpg").read_bytes(), "image/jpeg"))]
        r = c.post("/commons/image/remove-bg", files=files)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "image/png"
        assert r.headers["x-sc-cost"] == "2.0"
        # PNG signature
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
        w, h, total, non_t = _alpha_stats(r.content)
        assert w > 0 and h > 0
        # Substantial chunk should be transparent — portrait subject
        # occupies roughly 30% of the frame.
        assert non_t < total * 0.95, f"expected meaningful transparency, got {non_t}/{total}"
        assert non_t > total * 0.05, f"expected subject preserved, got {non_t}/{total}"


def test_remove_bg_batch():
    with _client() as c:
        body = (FIX / "portrait.jpg").read_bytes()
        files = [
            ("files", ("p1.jpg", body, "image/jpeg")),
            ("files", ("p2.jpg", body, "image/jpeg")),
        ]
        r = c.post("/commons/image/remove-bg/batch", files=files)
        assert r.status_code == 200, r.text
        assert r.headers["x-images"] == "2"
        assert r.headers["x-sc-cost"] == "4.0"
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = z.namelist()
        assert len(names) == 2
        assert all(n.endswith(".png") for n in names)
        # Each entry must have valid alpha channel
        for n in names:
            png = z.read(n)
            assert png[:8] == b"\x89PNG\r\n\x1a\n"
            w, h, total, non_t = _alpha_stats(png)
            assert non_t < total


def test_auth_rejected_without_key():
    r = httpx.post(f"{BASE}/commons/image/remove-bg", timeout=10)
    assert r.status_code in (401, 422)


def test_bad_extension_rejected():
    with _client() as c:
        files = [("file", ("evil.exe", b"MZ\x90\x00fake", "application/octet-stream"))]
        r = c.post("/commons/image/remove-bg", files=files)
        assert r.status_code == 400
