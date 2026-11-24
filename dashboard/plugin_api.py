"""Push pairing mounted behind the ordinary Hermes dashboard authentication."""

from pathlib import Path
import sys

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.append(_ROOT)

from butler_push import PLATFORM_NAME, PLUGIN_VERSION, RELAY_URL_ENV  # noqa: E402
from butler_push.hooks import legacy_sender_configured, relay_url_from_env  # noqa: E402
from butler_push.keys import load_or_create_keys  # noqa: E402
from butler_push.payload import PAYLOAD_VERSION  # noqa: E402

router = APIRouter()
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


@router.get("/capabilities")
def get_capabilities() -> JSONResponse:
    routes = sorted(f"{method} {route.path}" for route in router.routes
                    for method in getattr(route, "methods", ()))
    return JSONResponse({"plugin_version": PLUGIN_VERSION, "routes": routes}, headers=NO_STORE)


@router.get("/pairing")
def get_pairing() -> JSONResponse:
    relay_url = relay_url_from_env()
    if not relay_url:
        detail = ("Old Hermex Push relay is still configured; disable its sender first."
                  if legacy_sender_configured() else f"{RELAY_URL_ENV} is not configured")
        raise HTTPException(status_code=409, detail=detail, headers=NO_STORE)
    keys = load_or_create_keys()
    return JSONResponse(
        {
            "relay_url": relay_url,
            "install_key": keys.install_key,
            "preview_key": keys.preview_key_b64,
            "platform": PLATFORM_NAME,
            "payload_version": PAYLOAD_VERSION,
            "plugin_version": PLUGIN_VERSION,
        },
        headers=NO_STORE,
    )
