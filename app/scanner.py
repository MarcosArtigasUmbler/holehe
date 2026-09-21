"""Thin wrapper around holehe's module functions.

holehe is built on trio + httpx. Each check runs inside its own ``trio.run``
so it can be dispatched from an asyncio server through a worker thread.
"""
from __future__ import annotations

import inspect
import re
import time
from typing import Any

import httpx
import trio

from holehe.core import import_submodules

_MODULES: dict[str, Any] | None = None
_DOMAINS: dict[str, str] = {}
_DOMAIN_RE = re.compile(r"""^\s*domain\s*=\s*["']([^"']+)["']""", re.MULTILINE)


def load_modules() -> dict[str, Any]:
    """Discover every holehe site-check coroutine, keyed by module name."""
    global _MODULES
    if _MODULES is None:
        found: dict[str, Any] = {}
        for full_name, module in import_submodules("holehe.modules").items():
            parts = full_name.split(".")
            if len(parts) <= 3:
                continue
            site = parts[-1]
            func = module.__dict__.get(site)
            if func is not None and inspect.iscoroutinefunction(func):
                found[site] = func
                try:
                    m = _DOMAIN_RE.search(inspect.getsource(func))
                    if m:
                        _DOMAINS[site] = m.group(1)
                except OSError:
                    pass
        _MODULES = dict(sorted(found.items()))
    return _MODULES


PASSWORD_RECOVERY_MODULES = {"adobe", "mail_ru", "odnoklassniki", "samsung"}


def _normalize(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "domain": raw.get("domain") or _DOMAINS.get(name),
        "method": raw.get("method"),
        "exists": bool(raw.get("exists", False)),
        "rate_limited": bool(raw.get("rateLimit", False)),
        "error": bool(raw.get("error", False)),
        "frequent_rate_limit": bool(raw.get("frequent_rate_limit", False)),
        "email_recovery": raw.get("emailrecovery"),
        "phone_number": raw.get("phoneNumber"),
        "others": raw.get("others"),
    }


async def _run_one(name, func, email, client, results, sem):
    async with sem:
        out: list[dict[str, Any]] = []
        try:
            await func(email, client, out)
        except Exception as exc:  # module blew up (site changed, parse error, ...)
            results.append(
                _normalize(name, {"error": True, "others": {"errorMessage": repr(exc)[:200]}})
            )
            return
        if not out:
            results.append(_normalize(name, {"error": True}))
            return
        results.append(_normalize(name, out[0]))


async def _scan(email: str, timeout: float, modules: dict[str, Any], concurrency: int):
    results: list[dict[str, Any]] = []
    sem = trio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        async with trio.open_nursery() as nursery:
            for name, func in modules.items():
                nursery.start_soon(_run_one, name, func, email, client, results, sem)
    return results


def scan(
    email: str,
    timeout: float = 10.0,
    no_password_recovery: bool = False,
    modules: list[str] | None = None,
    concurrency: int = 50,
) -> dict[str, Any]:
    """Blocking entry point. Call it from a worker thread."""
    all_modules = load_modules()
    selected = all_modules
    if modules:
        selected = {n: all_modules[n] for n in modules if n in all_modules}
    if no_password_recovery:
        selected = {n: f for n, f in selected.items() if n not in PASSWORD_RECOVERY_MODULES}

    started = time.perf_counter()
    results = trio.run(_scan, email, timeout, selected, concurrency)
    results.sort(key=lambda r: r["name"])
    elapsed = round(time.perf_counter() - started, 2)

    used = [r for r in results if r["exists"]]
    return {
        "email": email,
        "elapsed_seconds": elapsed,
        "summary": {
            "checked": len(results),
            "used": len(used),
            "not_used": sum(1 for r in results if not r["exists"] and not r["rate_limited"] and not r["error"]),
            "rate_limited": sum(1 for r in results if r["rate_limited"]),
            "errors": sum(1 for r in results if r["error"]),
        },
        "used": [r["domain"] for r in used],
        "results": results,
    }
