"""GHunt integration: enrich a /check with public Google-account data.

GHunt (github.com/mxrch/GHunt, AGPL-3.0) reads what a Google account exposes
through Google's own internal People/Maps endpoints. It authenticates with a
session generated once via `ghunt login`, stored as a base64 blob.

This module is defensive by design: any failure (missing creds, expired
session, Google-side change, network error) is swallowed and reported in the
returned dict, so /check never fails because of GHunt.

Auth provisioning (pick one):
  - Mount the creds file at the path in GHUNT_CREDS_PATH
    (default ~/.malfrats/ghunt/creds.m), or
  - Set GHUNT_CREDS_B64 to the *contents* of that creds.m file; on first use
    it is written to GHUNT_CREDS_PATH.

Generate the blob once, on a machine with a throwaway Google account:
    pip install ghunt && ghunt login
    # then: base64 the resulting ~/.malfrats/ghunt/creds.m
"""
from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
from typing import Any

_ENABLED = os.getenv("GHUNT_ENABLED", "true").strip().lower() in ("1", "true", "yes")
_CREDS_PATH = Path(os.getenv("GHUNT_CREDS_PATH", str(Path.home() / ".malfrats" / "ghunt" / "creds.m")))
_TIMEOUT = float(os.getenv("GHUNT_TIMEOUT", "20"))

_INIT_DONE = False
_IMPORT_ERROR: str | None = None


def _materialize_creds() -> None:
    """Write GHUNT_CREDS_B64 to the creds path if the file is not already there."""
    if _CREDS_PATH.is_file():
        return
    blob = os.getenv("GHUNT_CREDS_B64", "").strip()
    if not blob:
        return
    _CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # The env var may hold the raw creds.m text, or that text base64-wrapped once more.
    try:
        decoded = base64.b64decode(blob, validate=True).decode()
        # creds.m is itself base64 of JSON; if decoded still looks like base64, it was double-wrapped
        content = decoded if decoded.strip().startswith("{") is False and _looks_b64(decoded) else blob
    except Exception:
        content = blob
    _CREDS_PATH.write_text(content, encoding="utf-8")


def _looks_b64(s: str) -> bool:
    s = s.strip()
    try:
        base64.b64decode(s, validate=True)
        return True
    except Exception:
        return False


def _ensure_init() -> bool:
    """Import GHunt and init its globals once. Returns True if usable."""
    global _INIT_DONE, _IMPORT_ERROR
    if _INIT_DONE:
        return _IMPORT_ERROR is None
    _INIT_DONE = True
    try:
        _materialize_creds()
        from ghunt import globals as gb  # noqa: F401
        gb.init_globals()
    except Exception as exc:  # pragma: no cover - depends on optional dep
        _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
        return False
    return True


def is_configured() -> bool:
    return _ENABLED and (_CREDS_PATH.is_file() or bool(os.getenv("GHUNT_CREDS_B64", "").strip()))


def _disabled(reason: str) -> dict[str, Any]:
    return {"enabled": False, "found": None, "reason": reason}


async def _probe(email: str) -> dict[str, Any]:
    import httpx

    from ghunt.objects.base import GHuntCreds
    from ghunt.apis.peoplepa import PeoplePaHttp
    from ghunt.helpers import auth, gmaps

    creds = GHuntCreds(creds_path=str(_CREDS_PATH))
    creds.load_creds(silent=True)

    async with httpx.AsyncClient(http2=True, timeout=_TIMEOUT) as client:
        # Refresh cookies/OSIDs if the stored session drifted.
        if not await auth.check_cookies(client, creds.cookies):
            await auth.gen_cookies_and_osids(client, creds)
            creds.save_creds(silent=True)

        people = PeoplePaHttp(creds)
        found, person = await people.people_lookup(client, email, params_template="max_details")
        if not found:
            return {"enabled": True, "found": False}

        container = "PROFILE"
        if container not in person.sourceIds:
            # Account exists but only in a private container, not a public profile.
            return {"enabled": True, "found": True, "public_profile": False, "gaia_id": person.personId}

        last_edit = person.sourceIds[container].lastUpdated
        photo = person.profilePhotos.get(container)
        services = person.inAppReachability[container].apps if container in person.inAppReachability else []

        result: dict[str, Any] = {
            "enabled": True,
            "found": True,
            "public_profile": True,
            "gaia_id": person.personId,
            "last_profile_edit": last_edit.isoformat() + "Z" if last_edit else None,
            "has_custom_profile_picture": bool(photo and not photo.isDefault),
            "profile_picture_url": photo.url if (photo and not photo.isDefault) else None,
            "activated_google_services": sorted(services),
            "is_enterprise": bool(person.extendedData.gplusData.isEntrepriseUser),
        }

        # Maps: this GHunt version returns aggregate counts only, not per-review dates.
        try:
            err, stats = await gmaps.get_reviews(client, person.personId)
            if not err and stats:
                result["maps"] = {
                    "reviews": stats.get("Reviews", 0),
                    "ratings": stats.get("Ratings", 0),
                    "photos": stats.get("Photos", 0),
                }
        except Exception:
            pass

        return result


def probe(email: str) -> dict[str, Any]:
    """Blocking entry point; call from a worker thread."""
    if not _ENABLED:
        return _disabled("GHUNT_ENABLED is false")
    if not is_configured():
        return _disabled("no GHunt credentials configured (set GHUNT_CREDS_B64 or mount creds.m)")
    if not _ensure_init():
        return _disabled(f"GHunt not available: {_IMPORT_ERROR}")
    try:
        return asyncio.run(_probe(email))
    except Exception as exc:
        return {"enabled": True, "found": None, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
