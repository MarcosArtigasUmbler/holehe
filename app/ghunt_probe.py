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


def _maps_contrib_urls(gaia_id: str) -> dict[str, str]:
    base = f"https://www.google.com/maps/contrib/{gaia_id}"
    return {"profile": base, "reviews": f"{base}/reviews", "photos": f"{base}/photos"}


# Current (2026) Maps "mas" reviews payload. {0}=gaia id. GHunt's own template is
# stale and returns no reviews; this one was recovered from the live Maps UI and
# works without a session token. The response is Google-internal, so it is parsed
# by scanning for microsecond epoch timestamps rather than by fixed indices.
_REVIEWS_PB = (
    "!1s{0}!2m3!1s!7e81!15i14414!6m2!4b1!7b1!10m5!1b1!5b1!9m1!1e3!11b1"
    "!14m60!1m49!1m5!1m4!1e1!1e3!1e2!1e4!3m5!2m4!3m3!1m2!1i260!2i365!4m1!3i10!10b1"
    "!11m33!1m3!1e1!2b0!3e3!1m3!1e2!2b1!3e2!1m3!1e2!2b0!3e3!1m3!1e8!2b0!3e3!1m3!1e10!2b0!3e3"
    "!1m3!1e10!2b1!3e2!1m3!1e10!2b0!3e4!1m3!1e9!2b1!3e2!2b1!2m5!1e1!1e4!1e5!1e3!1e2!3b1!4b1!5m1!1e1"
    "!17m28!1m6!1m2!1i0!2i0!2m2!1i530!2i768!1m6!1m2!1i974!2i0!2m2!1i1024!2i768!1m6!1m2!1i0!2i0!2m2!1i1024!2i20"
    "!1m6!1m2!1i0!2i748!2m2!1i1024!2i768!41m14!1i10!2m9!2b1!3b1!5b1!7b1!12m4!1b1!2b1!4m1!1e1!7m2!1m1!1e1"
)


def _scan_epoch_micros(obj: Any) -> list[int]:
    """Collect ints that look like microsecond epochs between ~2005 and ~2027."""
    found: list[int] = []

    def rec(o: Any) -> None:
        if isinstance(o, bool):
            return
        if isinstance(o, int):
            if 1_100_000_000_000_000 <= o <= 1_830_000_000_000_000:
                found.append(o)
        elif isinstance(o, list):
            for v in o:
                rec(v)
        elif isinstance(o, dict):
            for v in o.values():
                rec(v)

    rec(obj)
    return found


async def _maps_reviews(client, gaia_id: str) -> dict[str, Any]:
    """Oldest/newest public Maps review dates for the account.

    Each review carries a microsecond creation timestamp; the oldest is a strong
    'account existed by' floor. Only dates are extracted (not review text), and
    the parse degrades to empty if Google changes the response.
    """
    import json as _json
    from datetime import datetime, timezone

    pb = _REVIEWS_PB.format(gaia_id)
    req = await client.get(
        f"https://www.google.com/locationhistory/preview/mas?authuser=0&hl=en&gl=us&pb={pb}"
    )
    if req.status_code != 200:
        return {"error": f"status {req.status_code}"}
    try:
        data = _json.loads(req.text[5:])
    except Exception as exc:
        return {"error": f"parse: {type(exc).__name__}"}

    micros = _scan_epoch_micros(data)
    if not micros:
        return {"count_with_dates": 0, "oldest_date": None, "newest_date": None}
    to_iso = lambda m: datetime.fromtimestamp(m / 1_000_000, tz=timezone.utc).replace(tzinfo=None, microsecond=0).isoformat() + "Z"
    return {
        "count_with_dates": len(micros),
        "oldest_date": to_iso(min(micros)),
        "newest_date": to_iso(max(micros)),
    }


async def _maps_photos(client, gaia_id: str, cap: int = 40) -> dict[str, Any]:
    """Public photos the account contributed to Google Maps.

    Each photo has a URL, a real capture date and the place it was taken. The
    oldest photo date is a strong 'account existed by' floor. This parses
    Google's internal locationhistory/mas response, so it is guarded field by
    field and degrades to an empty list if Google changes the format.
    """
    import json as _json
    from datetime import datetime

    from ghunt import globals as gb

    photos: list[dict[str, Any]] = []
    token = ""
    pages = 0
    while True:
        if token:
            pb = gb.config.templates["gmaps_pb"]["photos"]["page"].format(gaia_id, token)
        else:
            pb = gb.config.templates["gmaps_pb"]["photos"]["first"].format(gaia_id)
        req = await client.get(
            f"https://www.google.com/locationhistory/preview/mas?authuser=0&hl=en&gl=us&pb={pb}"
        )
        if req.status_code != 200:
            break
        try:
            data = _json.loads(req.text[5:])
        except Exception:
            break
        if len(data) <= 22 or not data[22]:
            break
        block = data[22]
        items = block[1] if len(block) > 1 else None
        if not items:
            break
        for it in items:
            try:
                url = it[0][6][0].split("=")[0]
            except Exception:
                url = None
            date_iso = None
            try:
                d = it[0][21][6][8]
                date_iso = datetime(d[0], d[1], d[2], d[3] if len(d) > 3 and d[3] else 0).isoformat() + "Z"
            except Exception:
                pass
            place = None
            try:
                if len(it) > 1 and len(it[1]) > 2:
                    place = it[1][2]
            except Exception:
                pass
            if url:
                photos.append({"url": url, "date": date_iso, "place": place})
        pages += 1
        token = block[3] if len(block) > 3 and block[3] else ""
        if not token or pages >= 10 or len(photos) >= cap:
            break

    dates = [p["date"] for p in photos if p["date"]]
    return {
        "count": len(photos),
        "oldest_date": min(dates) if dates else None,
        "newest_date": max(dates) if dates else None,
        "items": photos[:cap],
    }


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

        # Maps: aggregate counts, public contributor URLs, and contributed photos
        # (each with a real capture date -> the oldest is a strong age floor).
        # Per-review extraction is not included: Google changed that response
        # shape and it no longer parses reliably. The reviews contributor page
        # URL below lets a consumer open the reviews directly.
        maps: dict[str, Any] = {"contributions_url": _maps_contrib_urls(person.personId)}
        try:
            err, stats = await gmaps.get_reviews(client, person.personId)
            if not err and stats:
                maps["reviews"] = stats.get("Reviews", 0)
                maps["ratings"] = stats.get("Ratings", 0)
                maps["photos"] = stats.get("Photos", 0)
        except Exception as exc:
            maps["stats_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        try:
            maps["contributed_photos"] = await _maps_photos(client, person.personId)
        except Exception as exc:
            maps["contributed_photos"] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
        try:
            maps["reviews_dates"] = await _maps_reviews(client, person.personId)
        except Exception as exc:
            maps["reviews_dates"] = {"error": f"{type(exc).__name__}: {str(exc)[:120]}"}
        result["maps"] = maps

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
        maps_block = result.get("maps") or {}
        for sub in ("contributed_photos", "reviews_dates"):
            blk = maps_block.get(sub) or {}
            if isinstance(blk, dict) and blk.get("oldest_date"):
                candidates.append(datetime.fromisoformat(blk["oldest_date"].rstrip("Z")))
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
