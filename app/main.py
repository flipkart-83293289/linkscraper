"""
FastAPI backend for the page-cloning service.

Endpoints:
  GET  /health          -> liveness check for Render
  POST /api/generate     -> accepts {"url": "..."} and returns a fully
                             self-contained HTML document (inline CSS/JS/images)

Concurrency model:
  Render's free tier gives you 512MB RAM total. A single headless Chromium
  instance with one page open comfortably uses 150-300MB depending on the
  page. Running two in parallel WILL crash the dyno. We therefore serialize
  all scrape jobs behind an asyncio.Semaphore(1) and enforce a hard timeout
  per job so a hung page can't wedge the whole service.
"""

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, HttpUrl, field_validator

from app.scraper import ScrapeError, scrape_and_inline, scrape_and_extract_metadata
from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("clone-service")

# Only one scrape job runs at a time -- this is the single most important
# guard against OOM kills on a 512MB instance.
JOB_SEMAPHORE = asyncio.Semaphore(1)

# In-memory job store. Fine for a single-instance free-tier deployment;
# if you ever scale to >1 instance you'd need Redis or similar instead.
JOBS: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Service starting up. Max concurrent scrape jobs = 1 (memory-constrained).")
    yield
    logger.info("Service shutting down.")


app = FastAPI(title="Page Clone Service", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


class GenerateRequest(BaseModel):
    url: HttpUrl

    @field_validator("url")
    @classmethod
    def block_private_hosts(cls, v: HttpUrl) -> HttpUrl:
        # Basic SSRF guard: refuse localhost / private-network targets so
        # this endpoint can't be used to probe your own Render network.
        host = v.host or ""
        blocked_prefixes = ("localhost", "127.", "10.", "192.168.", "169.254.", "0.")
        if host.startswith(blocked_prefixes) or host.endswith(".local"):
            raise ValueError("Target host is not allowed.")
        return v


class GenerateResponse(BaseModel):
    job_id: str
    status: str
    html_length: int | None = None
    error: str | None = None


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest):
    job_id = str(uuid.uuid4())
    url_str = str(req.url)
    logger.info(f"[{job_id}] Requested clone of {url_str}")

    if JOB_SEMAPHORE.locked():
        # Don't make the caller wait indefinitely behind a queue on a free
        # instance -- fail fast and let the frontend retry/poll instead.
        raise HTTPException(status_code=429, detail="Server busy processing another page. Try again shortly.")

    async with JOB_SEMAPHORE:
        try:
            html = await asyncio.wait_for(
                scrape_and_inline(url_str),
                timeout=settings.JOB_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error(f"[{job_id}] Timed out after {settings.JOB_TIMEOUT_SECONDS}s")
            raise HTTPException(status_code=504, detail="Page took too long to render/clone.")
        except ScrapeError as e:
            logger.error(f"[{job_id}] Scrape failed: {e}")
            raise HTTPException(status_code=422, detail=str(e))
        except Exception as e:
            logger.exception(f"[{job_id}] Unexpected failure")
            raise HTTPException(status_code=500, detail=f"Internal error: {e}")

    JOBS[job_id] = {"html": html}
    return GenerateResponse(job_id=job_id, status="done", html_length=len(html))


@app.get("/api/download/{job_id}", response_class=HTMLResponse)
async def download(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or already expired.")
    return HTMLResponse(content=job["html"], media_type="text/html")


@app.get("/api/view-source/{job_id}")
async def view_source(job_id: str):
    """
    Returns the generated HTML as plain text instead of rendering it --
    open this URL directly to see the raw single-file HTML/CSS/JS code
    (as opposed to /api/download, which renders it as a live page).
    """
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or already expired.")
    return HTMLResponse(content=job["html"], media_type="text/plain")


class MetadataRequest(BaseModel):
    url: HttpUrl

    @field_validator("url")
    @classmethod
    def block_private_hosts(cls, v: HttpUrl) -> HttpUrl:
        host = v.host or ""
        blocked_prefixes = ("localhost", "127.", "10.", "192.168.", "169.254.", "0.")
        if host.startswith(blocked_prefixes) or host.endswith(".local"):
            raise ValueError("Target host is not allowed.")
        return v


@app.post("/api/extract-metadata")
async def extract_metadata_endpoint(req: MetadataRequest):
    """
    Lightweight alternative to /api/generate: returns structured fields
    (title, price, currency, images, description, rating, availability)
    instead of a full offline HTML clone. Skips the asset-inlining pass
    entirely, so it's faster and uses less memory -- but it can only
    report what actually reached the page; if a site's bot-protection
    served a CAPTCHA/interstitial instead of the real page, the extracted
    fields will reflect that page, not the product.
    """
    url_str = str(req.url)

    if JOB_SEMAPHORE.locked():
        raise HTTPException(status_code=429, detail="Server busy processing another page. Try again shortly.")

    async with JOB_SEMAPHORE:
        try:
            metadata = await asyncio.wait_for(
                scrape_and_extract_metadata(url_str),
                timeout=settings.JOB_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Page took too long to render.")
        except ScrapeError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except Exception as e:
            logger.exception("Unexpected failure during metadata extraction")
            raise HTTPException(status_code=500, detail=f"Internal error: {e}")

    return metadata


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    logger.exception("Unhandled exception")
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})
