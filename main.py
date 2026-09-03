"""
MTProto Proxy Hub - FastAPI Application.
Main entry point with all routes.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import config, logger
from database import db
from http_logger import RequestLogger
from models import (
    ParseLinksRequest,
    ParseLinksResponse,
    PingStatus,
    PinRequest,
    ProxyCreate,
    ProxyListResponse,
    ProxyResponse,
    SortBy,
    StatsResponse,
    VoteRequest,
    VoteResponse,
)
from parser import ProxyLinkParser
from performance import RateLimiter, cache_store
from ping import PingChecker
from telethon_client import TelegramProxyListener

cleanup_task: asyncio.Task | None = None
ping_task: asyncio.Task | None = None


async def cleanup_worker() -> None:
    """Background worker that removes disliked proxies and old failed proxies every 30 minutes."""
    while True:
        try:
            deleted_count = await db.delete_most_disliked(min_dislikes=5)
            if deleted_count:
                logger.info(
                    "Deleted {count} disliked proxies",
                    count=deleted_count,
                )
                cache_store.invalidate("proxies")

            deleted_count = await db.delete_old_failed_proxies(days=2)
            if deleted_count:
                logger.info(
                    "Removed {count} old failed proxies",
                    count=deleted_count,
                )
                cache_store.invalidate("proxies")
        except Exception as exc:
            logger.exception("Cleanup worker failed: {error}", error=exc)
        await asyncio.sleep(30 * 60)


async def ping_worker() -> None:
    """Background worker that checks all proxies periodically."""
    while True:
        try:
            proxies = await db.get_all_for_ping()
            checked = 0
            for proxy_id, server, port, secret, proxy_type in proxies:
                result = await PingChecker.check(
                    server, port, secret, proxy_type
                )
                await db.update_ping(
                    proxy_id=proxy_id,
                    ping_ms=result.ping_ms,
                    ping_status=result.status,
                    tcp_ok=result.tcp_ok,
                    dns_ok=result.dns_ok,
                    is_fallback=result.is_fallback,
                    tcp_ping_ms=result.tcp_ping_ms,
                )
                checked += 1
                await asyncio.sleep(0.5)
            logger.info(
                "Ping cycle completed: {count} proxies checked", count=checked
            )
        except Exception as exc:
            logger.exception("Ping worker failed: {error}", error=exc)

        await asyncio.sleep(5 * 60)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Application lifespan: startup and shutdown."""
    global cleanup_task, ping_task

    await db.connect()
    cleanup_task = asyncio.create_task(cleanup_worker())
    ping_task = asyncio.create_task(ping_worker())

    if config.telegram.enabled:
        await TelegramProxyListener.start()
    else:
        logger.info("Telegram proxy listener is disabled in config")

    yield

    if cleanup_task:
        cleanup_task.cancel()
    if ping_task:
        ping_task.cancel()
    if config.telegram.enabled:
        await TelegramProxyListener.stop()
    await db.close()


app = FastAPI(
    title="MTProto Proxy Hub",
    description="Community-driven MTProto proxy aggregator",
    version="1.0.0",
    lifespan=lifespan,
)

# Add middleware in order: rate limit first, then logging
app.add_middleware(RequestLogger)
app.add_middleware(RateLimiter)

static_path = Path(__file__).parent / "static"
static_path.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=static_path), name="static")

templates_path = Path(__file__).parent / "templates"
templates_path.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=templates_path)


def get_voter_id(request: Request, response: Response) -> str:
    """Get or create voter ID from cookie."""
    voter_id = request.cookies.get("voter_id")
    if not voter_id:
        voter_id = str(uuid.uuid4())
        response.set_cookie(
            key="voter_id",
            value=voter_id,
            max_age=365 * 24 * 60 * 60,
            httponly=True,
            samesite="lax",
        )
    return voter_id


async def _validate_and_create_proxy(
    proxy: ProxyCreate,
) -> tuple[ProxyResponse | None, str | None]:
    """Validate proxy ping and add it to the database if reachable."""
    result = await PingChecker.check(
        proxy.server, proxy.port, proxy.secret, proxy.proxy_type
    )
    if result.status == PingStatus.FAILED:
        return None, "Proxy failed ping check"

    added = await db.add_proxy(proxy)
    if not added:
        return None, "Proxy already exists"

    await db.update_ping(
        proxy_id=added.id,
        ping_ms=result.ping_ms,
        ping_status=result.status,
        tcp_ok=result.tcp_ok,
        dns_ok=result.dns_ok,
        is_fallback=result.is_fallback,
        tcp_ping_ms=result.tcp_ping_ms,
    )
    return ProxyResponse.model_validate(added), None


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    sort: str = "likes",
) -> Response:
    """Main page with proxy list."""
    sort_by = (
        SortBy(sort) if sort in [s.value for s in SortBy] else SortBy.LIKES
    )
    proxies = await db.get_proxies(sort_by=sort_by, limit=100)
    total = await db.get_total_count()
    stats = await db.get_stats()

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "proxies": proxies,
            "total": total,
            "sort_by": sort_by.value,
            "stats": stats,
            # Cache-bust static assets by modification time
            "static_version": int(
                (Path(__file__).parent / "static" / "app.js").stat().st_mtime
            ),
        },
    )


@app.get("/api/proxies", response_model=ProxyListResponse)
async def list_proxies(
    sort: str = "likes",
    limit: int = 100,
    offset: int = 0,
) -> ProxyListResponse:
    """Get list of proxies."""
    sort_by = (
        SortBy(sort) if sort in [s.value for s in SortBy] else SortBy.LIKES
    )
    proxies = await db.get_proxies(sort_by=sort_by, limit=limit, offset=offset)
    total = await db.get_total_count()

    return ProxyListResponse(
        proxies=[ProxyResponse.model_validate(p) for p in proxies],
        total=total,
        sort_by=sort_by,
    )


@app.post("/api/proxies", response_model=ProxyResponse | None)
async def add_proxy(proxy: ProxyCreate) -> ProxyResponse | dict:
    """Add a single proxy."""
    result, error = await _validate_and_create_proxy(proxy)
    if error:
        if error == "Proxy already exists":
            raise HTTPException(status_code=409, detail=error)
        raise HTTPException(status_code=400, detail=error)
    return result


@app.post("/api/proxies/parse", response_model=ParseLinksResponse)
async def parse_links(data: ParseLinksRequest) -> ParseLinksResponse:
    """Parse proxy links from text."""
    proxies, errors = ProxyLinkParser.parse_text(data.text)
    return ParseLinksResponse(
        parsed=proxies,
        count=len(proxies),
        errors=errors,
    )


@app.post("/api/proxies/bulk")
async def add_bulk(data: ParseLinksRequest) -> dict:
    """Parse and add multiple proxies from text."""
    proxies, errors = ProxyLinkParser.parse_text(data.text)

    added = 0
    duplicates = 0
    results = []

    for proxy in proxies:
        result, error = await _validate_and_create_proxy(proxy)
        if result:
            added += 1
            results.append(
                {"address": f"{proxy.server}:{proxy.port}", "status": "added"}
            )
        elif error == "Proxy already exists":
            duplicates += 1
            results.append(
                {
                    "address": f"{proxy.server}:{proxy.port}",
                    "status": "duplicate",
                }
            )
        elif error:
            results.append(
                {
                    "address": f"{proxy.server}:{proxy.port}",
                    "status": "failed_checks",
                }
            )

    return {
        "added": added,
        "duplicates": duplicates,
        "errors": errors,
        "results": results,
    }


@app.post("/api/vote", response_model=VoteResponse)
async def vote(
    request: Request,
    response: Response,
    data: VoteRequest,
) -> VoteResponse:
    """Vote on a proxy."""
    voter_id = get_voter_id(request, response)

    proxy = await db.get_proxy(data.proxy_id)
    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")

    result = await db.vote(data.proxy_id, voter_id, data.vote_type)

    if result is None:
        return VoteResponse(
            success=False,
            likes=proxy.likes,
            dislikes=proxy.dislikes,
            message="Already voted",
        )

    likes, dislikes = result

    # Return new position when liked for dynamic re-sorting
    new_position = None
    if data.vote_type == "like":
        proxies = await db.get_proxies(
            sort_by=SortBy.LIKES, limit=100, offset=0
        )
        new_position = next(
            (i for i, p in enumerate(proxies) if p.id == data.proxy_id), -1
        )

    return VoteResponse(
        success=True,
        likes=likes,
        dislikes=dislikes,
        position=new_position,
    )


@app.post("/api/proxies/{proxy_id}/pin")
async def pin_proxy(
    proxy_id: int,
    data: PinRequest,
) -> dict:
    """Pin or unpin a proxy with password authentication."""
    if not config.app.password:
        raise HTTPException(
            status_code=403,
            detail="Pin password is not configured",
        )
    if data.password != config.app.password:
        raise HTTPException(status_code=403, detail="Invalid password")

    proxy = await db.get_proxy(proxy_id)
    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")

    await db.set_proxy_pinned(proxy_id, data.pinned)
    return {"success": True, "pinned": data.pinned}


@app.get("/api/vote/{proxy_id}")
async def get_user_vote(
    proxy_id: int,
    request: Request,
    response: Response,
) -> dict:
    """Get user's vote for a proxy."""
    voter_id = get_voter_id(request, response)
    vote = await db.get_vote(proxy_id, voter_id)
    return {"vote": vote}


@app.get("/api/stats", response_model=StatsResponse)
async def get_stats() -> StatsResponse:
    """Get aggregate statistics."""
    stats = await db.get_stats()
    return StatsResponse(**stats)


async def ping_proxy_async(
    proxy_id: int, server: str, port: int, secret: str, proxy_type: str
) -> None:
    """Background task to ping a newly added proxy."""
    try:
        result = await PingChecker.check(server, port, secret, proxy_type)
        await db.update_ping(
            proxy_id=proxy_id,
            ping_ms=result.ping_ms,
            ping_status=result.status,
            tcp_ok=result.tcp_ok,
            dns_ok=result.dns_ok,
            is_fallback=result.is_fallback,
            tcp_ping_ms=result.tcp_ping_ms,
        )
    except Exception as exc:
        logger.exception(
            "Auto-ping failed for proxy {proxy_id}",
            proxy_id=proxy_id,
            exc=exc,
        )


@app.post("/api/add-proxy")
async def add_proxy_api(data: dict) -> dict:
    """Add proxy via API (JSON)."""

    try:
        if "links" in data and data["links"].strip():
            return StreamingResponse(
                stream_proxy_import(data["links"]),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        if data.get("server") and data.get("port") and data.get("secret"):
            try:
                proxy = ProxyCreate(
                    server=data["server"],
                    port=int(data["port"]),
                    secret=data["secret"],
                    proxy_type=data.get("proxy_type", "mtproto"),
                )
                result, error = await _validate_and_create_proxy(proxy)
                if error and error != "Proxy already exists":
                    return {
                        "added": 0,
                        "duplicates": 0,
                        "errors": [error],
                    }
                if error == "Proxy already exists":
                    return {
                        "added": 0,
                        "duplicates": 1,
                        "errors": [],
                    }

            except ValueError as e:
                return {
                    "added": 0,
                    "duplicates": 0,
                    "errors": [str(e)],
                }
            else:
                return {
                    "added": 1 if result else 0,
                    "duplicates": 0 if result else 1,
                    "errors": [],
                }
        else:
            return {
                "added": 0,
                "duplicates": 0,
                "errors": ["No proxy data provided"],
            }
    except Exception as e:
        logger.exception("Failed to add proxy through API: {error}", error=e)
        return {
            "added": 0,
            "duplicates": 0,
            "errors": ["Internal error"],
        }


async def stream_proxy_import(text: str):
    """Stream one import result immediately after each proxy check."""
    proxies, errors = ProxyLinkParser.parse_text(text)
    added = 0
    duplicates = 0

    for error in errors:
        yield f"data: {json.dumps({'type': 'error', 'message': error})}\n\n"

    for proxy in proxies:
        address = f"{proxy.server}:{proxy.port}"
        result, error = await _validate_and_create_proxy(proxy)
        if result:
            added += 1
            status = "added"
        elif error == "Proxy already exists":
            duplicates += 1
            status = "duplicate"
        else:
            status = "failed_checks"

        yield f"data: {json.dumps({'type': 'result', 'address': address, 'status': status})}\n\n"

    yield f"data: {json.dumps({'type': 'done', 'added': added, 'duplicates': duplicates})}\n\n"


@app.post("/api/ping/{proxy_id}")
async def trigger_ping(proxy_id: int) -> dict:
    """Manually trigger ping check for a proxy."""
    proxy = await db.get_proxy(proxy_id)
    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")

    result = await PingChecker.check(
        proxy.server, proxy.port, proxy.secret, proxy.proxy_type
    )
    await db.update_ping(
        proxy_id=proxy_id,
        ping_ms=result.ping_ms,
        ping_status=result.status,
        tcp_ok=result.tcp_ok,
        dns_ok=result.dns_ok,
        is_fallback=result.is_fallback,
        tcp_ping_ms=result.tcp_ping_ms,
    )

    return {
        "ping_ms": result.ping_ms,
        "tcp_ping_ms": result.tcp_ping_ms,
        "status": result.status.value,
        "tcp_ok": result.tcp_ok,
        "dns_ok": result.dns_ok,
        "is_fallback": result.is_fallback,
    }


@app.post("/add", response_class=HTMLResponse)
async def add_proxy_form(
    request: Request,
    server: str = Form(default=""),
    port: int = Form(default=0),
    secret: str = Form(default=""),
    links: str = Form(default=""),
) -> Response:
    """Add proxy via form submission."""
    added = 0
    duplicates = 0
    errors: list[str] = []

    if links.strip():
        proxies, parse_errors = ProxyLinkParser.parse_text(links)
        errors.extend(parse_errors)

        for proxy in proxies:
            result, error = await _validate_and_create_proxy(proxy)
            if result:
                added += 1
            elif error == "Proxy already exists":
                duplicates += 1
            elif error:
                errors.append(f"{proxy.server}:{proxy.port} - {error}")

    elif server and port and secret:
        try:
            proxy = ProxyCreate(server=server, port=port, secret=secret)
            result, error = await _validate_and_create_proxy(proxy)
            if result:
                added += 1
            elif error == "Proxy already exists":
                duplicates += 1
            elif error:
                errors.append(error)
        except ValueError as e:
            errors.append(str(e))

    from starlette.responses import RedirectResponse

    message = f"Added: {added}"
    if duplicates:
        message += f", Duplicates: {duplicates}"
    if errors:
        message += f", Errors: {len(errors)}"

    return RedirectResponse(url=f"/?message={message}", status_code=303)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        access_log=False,  # Disable default Uvicorn access logs
        log_config=None,  # Use our custom logging config from config.py
    )
