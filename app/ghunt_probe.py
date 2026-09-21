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


def _dt(value: Any) -> str | None:
    """Serialize a GHunt datetime (or 0/None) to an ISO-8601 UTC string."""
    from datetime import datetime

    return value.isoformat() + "Z" if isinstance(value, datetime) else None


async def _play_games(creds, client, email: str) -> dict[str, Any] | None:
    """Public Play Games data. The oldest unlocked achievement is the strongest
    age floor GHunt exposes: it proves the account existed at least by that date."""
    from datetime import datetime

    from ghunt.apis.playgames import PlayGames
    from ghunt.apis.playgateway import PlayGatewayPaGrpc

    gateway = PlayGatewayPaGrpc(creds)
    search = await gateway.search_player(client, email)
    if not search.results:
        return {"found": False}

    player_id = search.results[0].id
    games = PlayGames(creds)
    found, profile = await games.get_profile(client, player_id)
    if not found:
        return {"found": False}
    if not profile.profile_settings.profile_visible:
        return {"found": True, "profile_public": False, "player_id": player_id}

    # Achievements come RECENT_FIRST, so the oldest is on the last page.
    unlock_dates: list[datetime] = []
    token, pages = "", 0
    while True:
        ok, token, page = await games.get_achievements(client, player_id, token)
        if not ok:
            break
        unlock_dates += [a.last_updated_timestamp for a in page.achievements
                         if isinstance(a.last_updated_timestamp, datetime)]
        pages += 1
        if not token or pages >= 30:
            break

    return {
        "found": True,
        "profile_public": True,
        "player_id": player_id,
        "gamertag": profile.gamertag or None,
        "level": profile.experience_info.current_level.level or None,
        "total_unlocked_achievements": profile.experience_info.total_unlocked_achievements,
        "last_played_game": profile.last_played_app.app_name or None,
        "last_played_at": _dt(profile.last_played_app.timestamp_millis),
        "last_level_up_at": _dt(profile.experience_info.last_level_up_timestamp_millis),
        "oldest_achievement_at": _dt(min(unlock_dates)) if unlock_dates else None,
        "newest_achievement_at": _dt(max(unlock_dates)) if unlock_dates else None,
    }


async def _calendar(creds, client, email: str) -> dict[str, Any] | None:
    """Public Google Calendar, if any. Events carry dates."""
    from ghunt.helpers import calendar as gcal

    found, calendar, events = await gcal.fetch_all(creds, client, email)
    if not found:
        return {"public": False}
    items = getattr(events, "items", []) or []
    out = []
    for ev in items[:20]:
        start = getattr(getattr(ev, "start", None), "date_time", None)
        out.append({"summary": getattr(ev, "summary", None), "start": _dt(start)})
    return {"public": True, "events": out}


async def _probe(email: str) -> dict[str, Any]:
    from datetime import datetime

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
        cover = person.coverPhotos.get(container)
        services = person.inAppReachability[container].apps if container in person.inAppReachability else []
        user_types = person.profileInfos[container].userTypes if container in person.profileInfos else []
        dynamite = person.extendedData.dynamiteData

        result: dict[str, Any] = {
            "enabled": True,
            "found": True,
            "public_profile": True,
            "gaia_id": person.personId,
            "last_profile_edit": _dt(last_edit),
            "has_custom_profile_picture": bool(photo and not photo.isDefault),
            "profile_picture_url": photo.url if (photo and not photo.isDefault) else None,
            "cover_photo_url": cover.url if (cover and not cover.isDefault) else None,
            "user_types": sorted(user_types),
            "activated_google_services": sorted(services),
            "is_enterprise": bool(person.extendedData.gplusData.isEntrepriseUser),
            "chat": {
                "entity_type": dynamite.entityType or None,
                "customer_id": dynamite.customerId or None,
            },
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
        except Exception as exc:
            result["maps"] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}

        try:
            result["play_games"] = await _play_games(creds, client, email)
        except Exception as exc:
            result["play_games"] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}

        try:
            result["calendar"] = await _calendar(creds, client, email)
        except Exception as exc:
            result["calendar"] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}

        # Earliest date across all dated evidence: the account provably existed by then.
        candidates: list[datetime] = []
        for value in (last_edit,):
            if isinstance(value, datetime):
                candidates.append(value)
        pg = result.get("play_games") or {}
        for key in ("oldest_achievement_at", "last_played_at", "last_level_up_at"):
            iso = pg.get(key) if isinstance(pg, dict) else None
            if iso:
                candidates.append(datetime.fromisoformat(iso.rstrip("Z")))
        cal = result.get("calendar") or {}
        for ev in (cal.get("events") or []):
            if ev.get("start"):
                candidates.append(datetime.fromisoformat(ev["start"].rstrip("Z")))
        result["account_existed_at_least_since"] = _dt(min(candidates)) if candidates else None

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
