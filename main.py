"""
MTProto Proxy Hub - FastAPI Application.
Main entry point with all routes.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import db
from models import (
    ParseLinksRequest,
    ParseLinksResponse,
    ProxyCreate,
    ProxyListResponse,
    ProxyResponse,
    SortBy,
    StatsResponse,
    VoteRequest,
    VoteResponse,
)
from parser import ProxyLinkParser
from ping import PingChecker

cleanup_task: asyncio.Task | None = None
ping_task: asyncio.Task | None = None


async def cleanup_worker() -> None:
    """Background worker that removes most disliked proxy every 30 minutes."""
    while True:
        await asyncio.sleep(30 * 60)
        try:
            deleted_id = await db.delete_most_disliked(min_dislikes=5)
            if deleted_id:
                print(f"[Cleanup] Deleted proxy {deleted_id}")
        except Exception as e:
            print(f"[Cleanup] Error: {e}")


async def ping_worker() -> None:
    """Background worker that checks all proxies periodically."""
    while True:
        try:
            proxies = await db.get_all_for_ping()
            for proxy_id, server, port in proxies:
                result = await PingChecker.check(server, port)
                await db.update_ping(
                    proxy_id=proxy_id,
                    ping_ms=result.ping_ms,
                    ping_status=result.status,
                    tcp_ok=result.tcp_ok,
                    dns_ok=result.dns_ok,
                )
                await asyncio.sleep(0.5)
        except Exception as e:
            print(f"[Ping] Error: {e}")

        await asyncio.sleep(5 * 60)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Application lifespan: startup and shutdown."""
    global cleanup_task, ping_task

    await db.connect()
    cleanup_task = asyncio.create_task(cleanup_worker())
    ping_task = asyncio.create_task(ping_worker())

    yield

    if cleanup_task:
        cleanup_task.cancel()
    if ping_task:
        ping_task.cancel()
    await db.close()


app = FastAPI(
    title="MTProto Proxy Hub",
    description="Community-driven MTProto proxy aggregator",
    version="1.0.0",
    lifespan=lifespan,
)


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


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    sort: str = "likes",
) -> Response:
    """Main page with proxy list."""
    sort_by = SortBy(sort) if sort in [s.value for s in SortBy] else SortBy.LIKES
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
        },
    )


@app.get("/api/proxies", response_model=ProxyListResponse)
async def list_proxies(
    sort: str = "likes",
    limit: int = 100,
    offset: int = 0,
) -> ProxyListResponse:
    """Get list of proxies."""
    sort_by = SortBy(sort) if sort in [s.value for s in SortBy] else SortBy.LIKES
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
    result = await db.add_proxy(proxy)
    if not result:
        raise HTTPException(status_code=409, detail="Proxy already exists")
    return ProxyResponse.model_validate(result)


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

    for proxy in proxies:
        result = await db.add_proxy(proxy)
        if result:
            added += 1
        else:
            duplicates += 1

    return {
        "added": added,
        "duplicates": duplicates,
        "errors": errors,
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
    return VoteResponse(
        success=True,
        likes=likes,
        dislikes=dislikes,
    )


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


async def ping_proxy_async(proxy_id: int, server: str, port: int) -> None:
    """Background task to ping a newly added proxy."""
    try:
        result = await PingChecker.check(server, port)
        await db.update_ping(
            proxy_id=proxy_id,
            ping_ms=result.ping_ms,
            ping_status=result.status,
            tcp_ok=result.tcp_ok,
            dns_ok=result.dns_ok,
        )
    except Exception as e:
        print(f"[Auto-ping] Error pinging proxy {proxy_id}: {e}")


@app.post("/api/add-proxy")
async def add_proxy_api(data: dict) -> dict:
    """Add proxy via API (JSON)."""
    added_proxies = []

    try:
        if "links" in data and data["links"].strip():
            proxies, errors = ProxyLinkParser.parse_text(data["links"])
            added = 0
            duplicates = 0
            for proxy in proxies:
                result = await db.add_proxy(proxy)
                if result:
                    added += 1
                    added_proxies.append(result)
                else:
                    duplicates += 1

            for proxy in added_proxies:
                asyncio.create_task(
                    ping_proxy_async(proxy.id, proxy.server, proxy.port)
                )

            return {
                "added": added,
                "duplicates": duplicates,
                "errors": errors,
            }
        if data.get("server") and data.get("port") and data.get("secret"):
            try:
                proxy = ProxyCreate(
                    server=data["server"],
                    port=int(data["port"]),
                    secret=data["secret"],
                )
                result = await db.add_proxy(proxy)

                if result:
                    asyncio.create_task(
                        ping_proxy_async(result.id, result.server, result.port)
                    )

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
        return {
            "added": 0,
            "duplicates": 0,
            "errors": [str(e)],
        }


@app.post("/api/ping/{proxy_id}")
async def trigger_ping(proxy_id: int) -> dict:
    """Manually trigger ping check for a proxy."""
    proxy = await db.get_proxy(proxy_id)
    if not proxy:
        raise HTTPException(status_code=404, detail="Proxy not found")

    result = await PingChecker.check(proxy.server, proxy.port)
    await db.update_ping(
        proxy_id=proxy_id,
        ping_ms=result.ping_ms,
        ping_status=result.status,
        tcp_ok=result.tcp_ok,
        dns_ok=result.dns_ok,
    )

    return {
        "ping_ms": result.ping_ms,
        "status": result.status.value,
        "tcp_ok": result.tcp_ok,
        "dns_ok": result.dns_ok,
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
            result = await db.add_proxy(proxy)
            if result:
                added += 1
            else:
                duplicates += 1

    elif server and port and secret:
        try:
            proxy = ProxyCreate(server=server, port=port, secret=secret)
            result = await db.add_proxy(proxy)
            if result:
                added += 1
            else:
                duplicates += 1
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

    uvicorn.run(app, host="0.0.0.0", port=8000)
