import asyncio
import os
import re
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from app import scanner

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

API_KEY = os.getenv("API_KEY", "").strip()
MAX_CONCURRENT_SCANS = int(os.getenv("MAX_CONCURRENT_SCANS", "4"))
DEFAULT_TIMEOUT = float(os.getenv("HOLEHE_TIMEOUT", "10"))
MODULE_CONCURRENCY = int(os.getenv("HOLEHE_MODULE_CONCURRENCY", "50"))

_scan_slots: asyncio.Semaphore


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _scan_slots
    _scan_slots = asyncio.Semaphore(MAX_CONCURRENT_SCANS)
    scanner.load_modules()  # warm import of ~120 modules
    yield


app = FastAPI(
    title="holehe API",
    version="1.0.0",
    description="Recebe um e-mail e retorna em quais plataformas ele possui conta (via holehe).",
    lifespan=lifespan,
)


async def require_api_key(x_api_key: Annotated[str | None, Header()] = None):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid or missing X-API-Key")


class CheckRequest(BaseModel):
    email: str = Field(..., examples=["someone@example.com"])
    timeout: float = Field(DEFAULT_TIMEOUT, ge=1, le=60, description="Timeout por site, em segundos")
    no_password_recovery: bool = Field(False, description="Pula sites que disparam e-mail de recuperação de senha")
    only_used: bool = Field(False, description="Retorna apenas os sites onde o e-mail existe")
    modules: list[str] | None = Field(None, description="Restringe a estes módulos (ver /modules)")


def _validate_email(email: str) -> str:
    email = email.strip()
    if not EMAIL_RE.fullmatch(email):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid email address")
    return email


async def _do_scan(req: CheckRequest):
    email = _validate_email(req.email)
    if req.modules:
        unknown = sorted(set(req.modules) - set(scanner.load_modules()))
        if unknown:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail={"unknown_modules": unknown})
    async with _scan_slots:
        result = await asyncio.to_thread(
            scanner.scan,
            email,
            req.timeout,
            req.no_password_recovery,
            req.modules,
            MODULE_CONCURRENCY,
        )
    if req.only_used:
        result["results"] = [r for r in result["results"] if r["exists"]]
    return result


@app.get("/health", tags=["meta"])
async def health():
    return {"status": "ok", "modules": len(scanner.load_modules())}


@app.get("/modules", tags=["meta"], dependencies=[Depends(require_api_key)])
async def modules():
    return {"count": len(scanner.load_modules()), "modules": list(scanner.load_modules())}


@app.post("/check", tags=["scan"], dependencies=[Depends(require_api_key)])
async def check_post(req: CheckRequest):
    return await _do_scan(req)


@app.get("/check", tags=["scan"], dependencies=[Depends(require_api_key)])
async def check_get(
    email: str = Query(..., examples=["someone@example.com"]),
    timeout: float = Query(DEFAULT_TIMEOUT, ge=1, le=60),
    no_password_recovery: bool = False,
    only_used: bool = False,
):
    return await _do_scan(
        CheckRequest(email=email, timeout=timeout, no_password_recovery=no_password_recovery, only_used=only_used)
    )
