# surface-commons-bg-remove

Open-source background remover — Commons guild's replacement for [remove.bg](https://remove.bg).

Strips backgrounds from `jpg/png/webp` uploads, returns a transparent PNG. Single-image and batch (zip) endpoints, SC-billed per call, audited to the Surface ledger.

## Endpoints

| Method | Path | Cost | Returns |
|---|---|---|---|
| `POST` | `/commons/image/remove-bg` | 2 SC | `image/png` (RGBA) |
| `POST` | `/commons/image/remove-bg/batch` | 2 SC × N | `application/zip` of PNGs |
| `GET`  | `/commons/image/health` | free | `application/json` capability probe |

## Stack

- [rembg](https://github.com/danielgatis/rembg) 2.0.75 + U2-Net ONNX (CPU)
- FastAPI router, attaches under any Surface app via `include_router`
- First call cold-starts the model (~170 MB → `~/.u2net/`); subsequent calls reuse a cached session

## Limits

- Single upload ≤ 10 MB (`BG_REMOVE_MAX_UPLOAD_BYTES`)
- Batch ≤ 20 images (`BG_REMOVE_MAX_BATCH`)
- Allowed: `image/jpeg`, `image/png`, `image/webp`

## Source of truth

This repo is a public mirror. Canonical home is `app/api/commons/bg_remove.py` in [Surface-V2](https://github.com/ratneshs230/Surface-V2). Pull-requests open against Surface-V2.

## License

MIT — see Surface Commons charter at [github.com/surface-commons](https://github.com/surface-commons).
