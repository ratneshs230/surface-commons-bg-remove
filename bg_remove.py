"""Background Remover — Commons guild's image-tooling wave.

Open-source replacement for remove.bg ($10/mo for 50 images, $99/mo for 500).
Single-image and batch endpoints. SC-billed per call, audited to `tool_calls`.

Stack: rembg (U2-Net ONNX, runs on CPU). First call cold-starts the model
(~170 MB to ~/.u2net/) — accept the latency.

Auth: service-key + X-Agent-Id (same as rest of `/api/v1/...`). The public
`/health` endpoint is unauthenticated so the Commons landing page can probe
availability.
"""
from __future__ import annotations

import io
import json
import os
import time
import uuid
import zipfile
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Response,
    UploadFile,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.service_auth import require_service_key_and_agent
from app.models.agent import Agent
from app.services.ledger import (
    InsufficientFundsError,
    record_transaction,
)


router = APIRouter(prefix="/commons/image", tags=["commons-image"])


SC_COST = {
    "remove_bg": float(os.environ.get("BG_REMOVE_SC", "2")),
    "remove_bg_batch_per_image": float(os.environ.get("BG_REMOVE_BATCH_SC", "2")),
}

MAX_UPLOAD_BYTES = int(os.environ.get("BG_REMOVE_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
MAX_BATCH_IMAGES = int(os.environ.get("BG_REMOVE_MAX_BATCH", "20"))
ALLOWED_MIMES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
ALLOWED_EXTS = (".jpg", ".jpeg", ".png", ".webp")

_session_cache: dict = {}


def _get_session():
    """Lazy-init rembg session — first call downloads ~170 MB U2-Net model."""
    sess = _session_cache.get("u2net")
    if sess is None:
        from rembg import new_session
        sess = new_session("u2net")
        _session_cache["u2net"] = sess
    return sess


def _capabilities() -> dict[str, bool]:
    caps = {"remove_bg": False, "model_loaded": False}
    try:
        import rembg  # noqa: F401
        caps["remove_bg"] = True
    except ImportError:
        return caps
    caps["model_loaded"] = "u2net" in _session_cache
    return caps


async def _read_upload(f: UploadFile, *, label: str) -> bytes:
    name = (f.filename or "").lower()
    if not name.endswith(ALLOWED_EXTS):
        raise HTTPException(
            status_code=400,
            detail={"error": "bad_extension", "field": label, "allowed": list(ALLOWED_EXTS)},
        )
    if f.content_type and f.content_type not in ALLOWED_MIMES:
        raise HTTPException(
            status_code=400,
            detail={"error": "bad_mime", "field": label, "got": f.content_type, "allowed": list(ALLOWED_MIMES)},
        )
    data = await f.read()
    if not data:
        raise HTTPException(status_code=400, detail=f"{label}_empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "upload_too_large",
                "limit_bytes": MAX_UPLOAD_BYTES,
                "got_bytes": len(data),
                "field": label,
            },
        )
    return data


async def _log_tool_call(
    db: AsyncSession,
    *,
    agent_id: str,
    tool_name: str,
    input_summary: dict,
    output_len: int,
    sc_cost: float,
    latency_ms: int,
    error: str | None = None,
) -> None:
    await db.execute(
        text(
            """
            INSERT INTO tool_calls
                (id, agent_id, tool_name, input_json, output_len,
                 sc_cost, latency_ms, error, created_at)
            VALUES
                (:id, :agent_id, :tool_name, CAST(:input_json AS JSONB),
                 :output_len, :sc_cost, :latency_ms, :error, NOW())
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "agent_id": agent_id,
            "tool_name": tool_name,
            "input_json": json.dumps(input_summary, default=str),
            "output_len": output_len,
            "sc_cost": sc_cost,
            "latency_ms": latency_ms,
            "error": error,
        },
    )


async def _charge(
    db: AsyncSession,
    agent: Agent,
    *,
    tool: str,
    cost: float,
    ref: str,
) -> float:
    if cost <= 0:
        return 0.0
    try:
        await record_transaction(
            db,
            agent_id=agent.id,
            delta=-cost,
            reason=f"commons.image.{tool}",
            ref_id=ref,
            note=f"Background Remover: {tool}",
        )
    except InsufficientFundsError as e:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_sc",
                "balance": e.balance,
                "needed": cost,
                "tool": f"commons.image.{tool}",
            },
        )
    return cost


def _strip_bg(data: bytes) -> tuple[bytes, dict]:
    """Run rembg on raw image bytes. Return (png_bytes, stats)."""
    from rembg import remove
    from PIL import Image

    sess = _get_session()
    out_bytes = remove(data, session=sess)

    im = Image.open(io.BytesIO(out_bytes))
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    alpha = im.split()[-1]
    total = im.width * im.height
    nontransparent = sum(1 for p in alpha.getdata() if p > 0) if total <= 4_000_000 else None

    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    body = buf.getvalue()

    return body, {
        "width": im.width,
        "height": im.height,
        "non_transparent_px": nontransparent,
        "out_bytes": len(body),
    }


@router.get("/health")
async def health() -> dict:
    caps = _capabilities()
    return {
        "service": "commons.image",
        "version": "1.0.0",
        "available": caps,
        "all_available": caps["remove_bg"],
        "sc_cost": SC_COST,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "max_batch_images": MAX_BATCH_IMAGES,
        "allowed_mimes": sorted(ALLOWED_MIMES),
    }


@router.post("/remove-bg")
async def remove_bg(
    file: Annotated[UploadFile, File(description="Image (jpg/png/webp, <=10MB) — background to be stripped")],
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> Response:
    t0 = time.monotonic()
    ref = str(uuid.uuid4())
    data = await _read_upload(file, label="file")
    in_size = len(data)

    cost = SC_COST["remove_bg"]
    await _charge(db, agent, tool="remove_bg", cost=cost, ref=ref)

    try:
        try:
            body, stats = _strip_bg(data)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail={"error": "remove_bg_failed", "msg": str(e)[:300]},
            )

        await _log_tool_call(
            db, agent_id=agent.id, tool_name="commons.image.remove_bg",
            input_summary={"in_bytes": in_size, **stats},
            output_len=len(body),
            sc_cost=cost,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()

        return Response(
            content=body,
            media_type="image/png",
            headers={
                "Content-Disposition": 'attachment; filename="no-bg.png"',
                "X-SC-Cost": str(cost),
                "X-Width": str(stats["width"]),
                "X-Height": str(stats["height"]),
                "X-In-Bytes": str(in_size),
                "X-Out-Bytes": str(len(body)),
            },
        )
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail={"error": "remove_bg_failed", "msg": str(e)[:200]})


@router.post("/remove-bg/batch")
async def remove_bg_batch(
    files: Annotated[list[UploadFile], File(description="1–N images (jpg/png/webp, <=10MB each)")],
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> Response:
    if not files:
        raise HTTPException(status_code=400, detail="need_at_least_1_image")
    if len(files) > MAX_BATCH_IMAGES:
        raise HTTPException(
            status_code=400,
            detail={"error": "batch_too_large", "limit": MAX_BATCH_IMAGES, "got": len(files)},
        )

    t0 = time.monotonic()
    ref = str(uuid.uuid4())

    per = SC_COST["remove_bg_batch_per_image"]
    total_cost = per * len(files)
    await _charge(db, agent, tool="remove_bg_batch", cost=total_cost, ref=ref)

    in_sizes: list[int] = []
    out_sizes: list[int] = []
    try:
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, f in enumerate(files):
                data = await _read_upload(f, label=f"file_{i}")
                in_sizes.append(len(data))
                try:
                    body, _stats = _strip_bg(data)
                except Exception as e:
                    raise HTTPException(
                        status_code=500,
                        detail={"error": "remove_bg_failed", "file_index": i, "msg": str(e)[:200]},
                    )
                stem = (f.filename or f"image_{i}").rsplit(".", 1)[0] or f"image_{i}"
                zf.writestr(f"{i + 1:03d}_{stem}.png", body)
                out_sizes.append(len(body))
        body = zip_buf.getvalue()

        await _log_tool_call(
            db, agent_id=agent.id, tool_name="commons.image.remove_bg_batch",
            input_summary={
                "image_count": len(files),
                "in_bytes": in_sizes,
                "out_bytes": out_sizes,
                "per_image_sc": per,
            },
            output_len=len(body),
            sc_cost=total_cost,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()

        return Response(
            content=body,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="no-bg.zip"',
                "X-SC-Cost": str(total_cost),
                "X-SC-Per-Image": str(per),
                "X-Images": str(len(files)),
            },
        )
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail={"error": "batch_failed", "msg": str(e)[:200]})
