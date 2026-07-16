#!/usr/bin/env python3
"""Internal registration + captcha sidecar for the Go main process.

Public API traffic must not hit this service. Go calls:

  /internal/registration/v1/*

Python owns registration machine execution, mailbox providers, and Turnstile
solving. This process intentionally reuses grok2api.upstream.grok_build_adapter
instead of reimplementing browser/captcha logic.
"""

from __future__ import annotations

import os
import secrets
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

try:
    from grok2api.upstream import grok_build_adapter as reg
except Exception as exc:  # noqa: BLE001
    reg = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(exc)
else:
    _IMPORT_ERROR = None


app = FastAPI(title="grok2api registration internal API", version="1.0.0")
API_PREFIX = "/internal/registration/v1"


def _require_auth(request: Request) -> None:
    expected = (os.environ.get("GROK2API_REGISTRATION_TOKEN") or "").strip()
    if not expected:
        return
    auth = (request.headers.get("authorization") or "").strip()
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="registration token required")
    token = auth[7:].strip()
    if not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="invalid registration token")


def _adapter():
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail=f"registration adapter unavailable: {_IMPORT_ERROR or 'import failed'}",
        )
    return reg


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": reg is not None,
        "service": "registration-sidecar",
        "adapter_error": _IMPORT_ERROR,
    }


@app.get(f"{API_PREFIX}/availability")
def availability(request: Request) -> dict[str, Any]:
    _require_auth(request)
    adapter = _adapter()
    return adapter.registration_available()


@app.post(f"{API_PREFIX}/jobs")
async def start_job(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    _require_auth(request)
    adapter = _adapter()
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be object")
    # Idempotency key is accepted for contract compatibility; adapter currently
    # relies on its own session/batch ids.
    _ = idempotency_key
    kwargs = {
        k: body.get(k)
        for k in (
            "captcha_provider",
            "local_solver_url",
            "yescaptcha_key",
            "proxy",
            "proxy_username",
            "proxy_password",
            "proxy_strategy",
            "moemail_api_key",
            "moemail_base_url",
            "prefix",
            "domain",
            "expiry_ms",
            "mail_provider",
            "count",
            "concurrency",
            "stagger_ms",
            "probe_delay_sec",
        )
        if k in body
    }
    result = adapter.start_registration(**kwargs)
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="invalid registration response")
    if result.get("ok") is False:
        raise HTTPException(status_code=400, detail=str(result.get("error") or "registration failed"))
    return result


@app.get(f"{API_PREFIX}/sessions")
def list_sessions(request: Request) -> dict[str, Any]:
    _require_auth(request)
    adapter = _adapter()
    return adapter.list_registration_sessions()


@app.get(f"{API_PREFIX}/sessions/{{session_id}}")
def get_session(session_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    adapter = _adapter()
    include_auth = (request.query_params.get("include_auth_json") or "").strip() in {
        "1",
        "true",
        "yes",
    }
    sess = adapter.get_registration_session(session_id, include_auth_json=include_auth)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    return sess


@app.post(f"{API_PREFIX}/sessions/{{session_id}}/stop")
def stop_session(session_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    adapter = _adapter()
    return adapter.stop_registration_session(session_id)


@app.get(f"{API_PREFIX}/batches/{{batch_id}}")
def get_batch(batch_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    adapter = _adapter()
    batch = adapter.get_registration_batch(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="batch not found")
    return batch


@app.post(f"{API_PREFIX}/batches/{{batch_id}}/resume")
async def resume_batch(batch_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    adapter = _adapter()
    force = False
    try:
        body = await request.json()
        if isinstance(body, dict):
            force = bool(body.get("force"))
    except Exception:
        force = False
    return adapter.resume_registration_batch(batch_id, force=force)


@app.post(f"{API_PREFIX}/batches/{{batch_id}}/stop")
def stop_batch(batch_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    adapter = _adapter()
    return adapter.stop_registration_batch(batch_id)


@app.post(f"{API_PREFIX}/reclaim")
async def reclaim(request: Request) -> dict[str, Any]:
    _require_auth(request)
    adapter = _adapter()
    auto_resume = True
    try:
        body = await request.json()
        if isinstance(body, dict) and "auto_resume" in body:
            auto_resume = bool(body.get("auto_resume"))
    except Exception:
        pass
    # Prefer batch reclaim which also reclaims sessions.
    fn = getattr(adapter, "reclaim_orphaned_registration_batches", None)
    if callable(fn):
        # signature may not take auto_resume; call best-effort
        try:
            return fn(auto_resume=auto_resume)  # type: ignore[misc]
        except TypeError:
            return fn()
    fn2 = getattr(adapter, "reclaim_orphaned_registration_sessions", None)
    if callable(fn2):
        return fn2()
    return {"ok": True, "reclaimed": 0}


@app.post(f"{API_PREFIX}/stop")
def stop_all(request: Request) -> dict[str, Any]:
    _require_auth(request)
    adapter = _adapter()
    return adapter.stop_all_active_registrations()


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


def main() -> None:
    import uvicorn

    host = os.environ.get("GROK2API_REGISTRATION_HOST", "127.0.0.1")
    port = int(os.environ.get("GROK2API_REGISTRATION_PORT", "18070") or 18070)
    uvicorn.run(
        "scripts.registration_service:app",
        host=host,
        port=port,
        log_level=os.environ.get("GROK2API_REGISTRATION_LOG", "info"),
        factory=False,
    )


if __name__ == "__main__":
    # Support both `python scripts/registration_service.py` and module import.
    import uvicorn

    host = os.environ.get("GROK2API_REGISTRATION_HOST", "127.0.0.1")
    port = int(os.environ.get("GROK2API_REGISTRATION_PORT", "18070") or 18070)
    uvicorn.run(app, host=host, port=port, log_level="info")
