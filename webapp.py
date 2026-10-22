"""Telegram Mini App: a small aiohttp server (UI + JSON API) exposed through a Cloudflare quick tunnel.

The bot core (bot.py) is passed in as a module object, so the app shares its storage, Vinted
clients and helpers instead of importing a second copy of them.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional, Tuple
from urllib.parse import parse_qsl

from aiohttp import web
from telegram import MenuButtonDefault, MenuButtonWebApp, WebAppInfo

log = logging.getLogger("webapp")

HOST, PORT = "127.0.0.1", int(os.getenv("WEBAPP_PORT", "8787"))
TUNNEL_METRICS = os.getenv("TUNNEL_METRICS", "http://127.0.0.1:20241")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")
INIT_DATA_MAX_AGE = 7 * 86400

core = None  # the bot module, set in start()
public_url = {"value": None}
runtime = {}  # live objects (process, server); kept out of bot_data, which is pickled


# --- auth ---

def verify_init_data(init_data: str, token: str) -> Tuple[Optional[dict], str]:
    """Validate Telegram WebApp initData (HMAC signed with the bot token); returns (user, problem)."""
    if not init_data:
        return None, "no initData (opened outside Telegram?)"
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError as e:
        return None, f"unparsable initData: {e}"
    received = pairs.pop("hash", None)
    if not received:
        return None, "initData without hash"
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None, f"bad signature (fields: {','.join(sorted(pairs))})"
    if time.time() - int(pairs.get("auth_date", 0)) > INIT_DATA_MAX_AGE:
        return None, "initData expired"
    try:
        return json.loads(pairs.get("user", "null")), ""
    except ValueError:
        return None, "bad user field"


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        log.info("page %s from %s", request.path, request.headers.get("User-Agent", "?")[:80])
        return await handler(request)
    user, problem = verify_init_data(request.headers.get("X-Init-Data", ""), core.TOKEN)
    if not user:
        log.warning("api %s rejected: %s", request.path, problem)
        return web.json_response({"error": "unauthorized"}, status=401)
    log.info("api %s %s user=%s", request.method, request.path, user.get("id"))
    if core.ALLOWED and user["id"] not in core.ALLOWED:
        return web.json_response({"error": "forbidden"}, status=403)
    # In a private chat with the bot, chat id == user id.
    request["chat_id"] = user["id"]
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:
        log.exception("api %s failed", request.path)
        return web.json_response({"error": "server error"}, status=500)


# --- serializers ---

def filter_json(f) -> dict:
    flag, region_name, sym = core.region(f["domain"])
    return {
        "id": f["id"], "brand_id": f["brand_id"], "brand": f["brand_title"],
        "price_from": f["price_from"], "price_to": f["price_to"],
        "domain": f["domain"], "region": core.region_code(f["domain"]), "flag": flag,
        "region_name": region_name, "currency": sym,
        "keyword": f["keyword"] or "", "category": f["category"] or 0,
        "conditions": core.conditions_of(f), "sizes": core.sizes_of(f), "sizes_label": core.sizes_label(f),
        "enabled": bool(f["enabled"]),
        "found": core.storage.found_count(f["chat_id"], f["id"]),
    }


def item_json(r, chat_id: int, posted: Optional[float] = None) -> dict:
    d = dict(r)
    domain = d["url"].split("/")[2]
    return {
        "item_id": d["item_id"], "title": d["title"], "price": d["price"], "currency": d["currency"],
        "brand": d["brand"], "details": d["details"], "url": d["url"], "photo": d["photo"],
        "seller": d.get("seller_login"), "seller_id": d.get("seller_id"),
        "feedback": d.get("seller_feedback"), "rating": round((d.get("seller_reputation") or 0) * 5, 1),
        "ts": posted or d.get("found_at"), "flag": core.region(domain)[0],
        "fav": core.storage.is_favorite(chat_id, d["item_id"]),
    }


def body_float(v) -> Optional[float]:
    if v in (None, ""):
        return None
    return float(v)


# --- handlers ---

async def client_log(request):
    """Errors reported by the page itself (it can't reach the API if auth or rendering breaks)."""
    try:
        body = (await request.text())[:600]
    except Exception:
        body = "?"
    log.warning("client: %s | %s", body, request.headers.get("User-Agent", "?")[:80])
    return web.json_response({"ok": True})


async def index(request):
    return web.FileResponse(os.path.join(STATIC_DIR, "index.html"),
                            headers={"Cache-Control": "no-store"})


async def api_state(request):
    chat_id = request["chat_id"]
    chat = core.storage.chat(chat_id)
    today = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
    return web.json_response({
        "filters": [filter_json(f) for f in core.storage.filters_for(chat_id)],
        "settings": {
            "min_feedback": core.min_feedback_of(chat), "quiet": bool(chat["quiet"]),
            "paused": bool(chat["paused"]),
        },
        "stats": {
            "today": core.storage.found_count(chat_id, since=today),
            "favorites": len(core.storage.favorites_for(chat_id)),
            "last_check": core.state["last_check"].strftime("%H:%M") if core.state["last_check"] else None,
            "interval_min": max(core.CHECK_INTERVAL // 60, 1),
        },
        "options": {
            "regions": [{"code": c, "label": l} for c, l in core.REGION_CHOICES],
            "categories": [{"id": k, "label": v} for k, v in core.CATEGORIES.items()],
            "conditions": [{"id": k, "label": v} for k, v in core.CONDITIONS.items()],
            "size_groups": [{"key": key, "label": label,
                             "sizes": [{"id": z[0], "eu": z[1], "uk": z[2] if len(z) > 2 else z[1]} for z in sizes]}
                            for key, label, sizes in core.SIZE_GROUPS],
            "min_feedback": core.MIN_FEEDBACK_CHOICES,
            "popular": core.POPULAR_BRANDS,
            "periods": [{"hours": h, "label": l} for h, l in core.PERIODS],
            "quiet_hours": f"{core.QUIET_FROM}:00–{core.QUIET_TO:02d}:00",
        },
    })


async def api_items(request):
    chat_id = request["chat_id"]
    kind = request.query.get("kind", "feed")
    if kind == "fav":
        rows = core.storage.favorites_for(chat_id)
    elif kind.startswith("flt:"):
        rows = core.storage.found_for(chat_id, int(kind[4:]))
    else:
        rows = core.storage.found_for(chat_id)
    return web.json_response({"items": [item_json(r, chat_id) for r in rows]})


async def api_brands(request):
    q = request.query.get("q", "").strip()
    if len(q) < 2:
        return web.json_response({"brands": []})
    domain = core.chat_domain(request["chat_id"])
    try:
        brands = await core.vinted(domain).search_brands(q)
    except Exception as e:
        log.warning("brand search failed: %s", e)
        return web.json_response({"error": "vinted unavailable"}, status=502)
    return web.json_response({"brands": brands[:10]})


async def api_create_filter(request):
    chat_id = request["chat_id"]
    b = await request.json()
    domains = core.REGION_CODES.get(b.get("region"), [core.VINTED_DOMAIN])
    conditions = ",".join(str(int(c)) for c in b.get("conditions") or []) or None
    sizes = ",".join(str(int(z)) for z in b.get("sizes") or []) or None
    ids = []
    for d in domains:
        fid = core.storage.add_filter(chat_id, int(b["brand_id"]), b["brand"][:80],
                                      body_float(b.get("price_from")), body_float(b.get("price_to")), d)
        if b.get("keyword"):
            core.storage.set_option(chat_id, fid, "keyword", b["keyword"][:60])
        if b.get("category"):
            core.storage.set_option(chat_id, fid, "category", int(b["category"]))
        if conditions:
            core.storage.set_option(chat_id, fid, "conditions", conditions)
        if sizes:
            core.storage.set_option(chat_id, fid, "sizes", sizes)
        ids.append(fid)
    core.storage.set_chat_domain(chat_id, domains[0])
    return web.json_response({"ids": ids})


async def api_update_filter(request):
    chat_id = request["chat_id"]
    fid = int(request.match_info["fid"])
    f = core.storage.get_filter(chat_id, fid)
    if not f:
        raise web.HTTPNotFound()
    b = await request.json()
    if "price_from" in b or "price_to" in b:
        pf = body_float(b.get("price_from", f["price_from"]))
        pt = body_float(b.get("price_to", f["price_to"]))
        if (pf, pt) != (f["price_from"], f["price_to"]):
            core.storage.set_price(chat_id, fid, pf, pt)
    if "region" in b:
        domain = core.REGION_CODES.get(b["region"], [f["domain"]])[0]
        if domain != f["domain"]:
            core.storage.set_filter_domain(chat_id, fid, domain)
    if "keyword" in b and (b["keyword"] or None) != f["keyword"]:
        core.storage.set_option(chat_id, fid, "keyword", (b["keyword"] or "")[:60] or None)
    if "category" in b and (int(b["category"] or 0) or None) != f["category"]:  # negative = group
        core.storage.set_option(chat_id, fid, "category", int(b["category"] or 0) or None)
    if "conditions" in b:
        conditions = ",".join(str(int(c)) for c in b["conditions"]) or None
        if conditions != f["conditions"]:
            core.storage.set_option(chat_id, fid, "conditions", conditions)
    if "sizes" in b:
        sizes = ",".join(str(int(z)) for z in b["sizes"]) or None
        if sizes != f["sizes"]:
            core.storage.set_option(chat_id, fid, "sizes", sizes)
    if "enabled" in b and bool(b["enabled"]) != bool(f["enabled"]):
        core.storage.toggle_filter(chat_id, fid)
    return web.json_response(filter_json(core.storage.get_filter(chat_id, fid)))


async def api_delete_filter(request):
    core.storage.delete_filter(request["chat_id"], int(request.match_info["fid"]))
    return web.json_response({"ok": True})


async def api_all_filters(request):
    chat_id = request["chat_id"]
    b = await request.json()
    if b.get("action") == "delete":
        core.storage.delete_all_filters(chat_id)
    else:
        core.storage.set_all_enabled(chat_id, b.get("action") == "enable")
    return web.json_response({"ok": True})


async def api_favorite(request):
    chat_id = request["chat_id"]
    b = await request.json()
    item_id = int(b["item_id"])
    if core.storage.get_found(chat_id, item_id) or core.storage.is_favorite(chat_id, item_id):
        saved = core.storage.toggle_favorite(chat_id, item_id)
    else:
        # A listing from a period search: it isn't in the feed, so save the snapshot sent by the app.
        it = b.get("item") or {}
        core.storage.save_favorite(chat_id, {
            "item_id": item_id, "title": it.get("title", ""), "price": it.get("price", 0),
            "currency": it.get("currency", ""), "brand": it.get("brand", ""), "details": it.get("details", ""),
            "url": it.get("url", ""), "photo": it.get("photo"), "seller_login": it.get("seller") or "",
            "seller_feedback": it.get("feedback") or 0, "seller_reputation": (it.get("rating") or 0) / 5,
            "found_at": it.get("ts") or time.time(),
        })
        saved = True
    return web.json_response({"fav": saved})


async def api_block(request):
    chat_id = request["chat_id"]
    b = await request.json()
    seller_id, login = b.get("seller_id"), b.get("seller")
    if not seller_id and b.get("item_id"):
        r = core.storage.get_found(chat_id, int(b["item_id"]))
        if r:
            seller_id, login = r["seller_id"], r["seller_login"]
    if not seller_id:
        return web.json_response({"error": "unknown seller"}, status=400)
    core.storage.block_seller(chat_id, int(seller_id), login or str(seller_id))
    return web.json_response({"ok": True})


async def api_blocked(request):
    rows = core.storage.blocked_for(request["chat_id"])
    return web.json_response({"blocked": [{"seller_id": r["seller_id"], "seller": r["seller_login"]} for r in rows]})


async def api_unblock(request):
    core.storage.unblock_seller(request["chat_id"], int(request.match_info["sid"]))
    return web.json_response({"ok": True})


async def api_settings(request):
    chat_id = request["chat_id"]
    b = await request.json()
    if "min_feedback" in b:
        core.storage.set_min_feedback(chat_id, int(b["min_feedback"]))
    if "quiet" in b:
        core.storage.set_quiet(chat_id, bool(b["quiet"]))
    if "paused" in b:
        core.storage.set_paused(chat_id, bool(b["paused"]))
    return web.json_response({"ok": True})


async def api_clear_feed(request):
    core.storage.clear_found(request["chat_id"])
    return web.json_response({"ok": True})


async def api_check(request):
    found = await core.run_checks(request.app["tg"], request["chat_id"])
    return web.json_response({"found": found})


async def api_period(request):
    chat_id = request["chat_id"]
    scope = request.query.get("scope", "all")
    hours = int(request.query.get("hours", "1"))
    items = await core.period_search(chat_id, scope, hours)
    out = []
    for d in items:
        posted = core.posted_at(d["item_id"])
        out.append({
            "item_id": d["item_id"], "title": d["title"], "price": d["price"], "currency": d["currency"],
            "brand": d["brand"], "details": d["details"], "url": d["url"], "photo": d["photo"],
            "user_id": d["user_id"], "domain": d["domain"], "ts": posted, "approx": True,
            "flag": core.region(d["domain"])[0], "fav": core.storage.is_favorite(chat_id, d["item_id"]),
        })
    return web.json_response({"items": out})


async def api_seller(request):
    """Seller check for period results, one at a time (the profile API rate-limits bursts)."""
    chat_id = request["chat_id"]
    user_id = int(request.query["user_id"])
    domain = request.query.get("domain", core.VINTED_DOMAIN)
    if domain not in core.REGIONS:
        raise web.HTTPBadRequest()
    async with core.lock("seller"):
        try:
            s = await core.vinted(domain).seller(user_id)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)
        await asyncio.sleep(0.3)
    min_fb = core.min_feedback_of(core.storage.chat(chat_id))
    return web.json_response({
        "seller": s.login, "feedback": s.feedback_count, "rating": round(s.reputation * 5, 1),
        "ok": s.feedback_count >= min_fb and not core.storage.is_blocked(chat_id, user_id),
    })


def build_app(tg_app) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["tg"] = tg_app
    app.router.add_get("/", index)
    app.router.add_post("/log", client_log)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/items", api_items)
    app.router.add_get("/api/brands", api_brands)
    app.router.add_post("/api/filters", api_create_filter)
    app.router.add_post("/api/filters/all", api_all_filters)
    app.router.add_patch("/api/filters/{fid:\\d+}", api_update_filter)
    app.router.add_delete("/api/filters/{fid:\\d+}", api_delete_filter)
    app.router.add_post("/api/favorite", api_favorite)
    app.router.add_post("/api/block", api_block)
    app.router.add_get("/api/blocked", api_blocked)
    app.router.add_delete("/api/blocked/{sid:\\d+}", api_unblock)
    app.router.add_post("/api/settings", api_settings)
    app.router.add_post("/api/clear_feed", api_clear_feed)
    app.router.add_post("/api/check", api_check)
    app.router.add_get("/api/period", api_period)
    app.router.add_get("/api/seller", api_seller)
    return app


# --- tunnel ---
# cloudflared runs as its own launchd service (see autostart-mac.sh), so restarting the bot doesn't change
# the public URL: Telegram clients cache the menu button URL, and a dead one opens as a blank page.
# The bot asks cloudflared for its current hostname and keeps the menu button pointing at it.

async def watch_tunnel(tg_app):
    import aiohttp
    warned = False
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
        while True:
            try:
                async with http.get(f"{TUNNEL_METRICS}/quicktunnel") as r:
                    host = (await r.json(content_type=None)).get("hostname")
                url = f"https://{host}" if host else None
                warned = False
            except Exception as e:
                url = None
                if not warned:
                    log.warning("tunnel service not reachable at %s (%s); is com.vinted-bot.tunnel running?",
                                TUNNEL_METRICS, e)
                    warned = True
            if url and url != public_url["value"]:
                public_url["value"] = url
                log.info("mini app url: %s", url)
                await set_menu_button(tg_app)
            await asyncio.sleep(30)


async def set_menu_button(tg_app):
    url = public_url["value"]
    try:
        if url:
            await tg_app.bot.set_chat_menu_button(menu_button=MenuButtonWebApp("📱 Приложение", WebAppInfo(url)))
        else:
            await tg_app.bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except Exception as e:
        log.warning("set menu button failed: %s", e)


async def start(tg_app, bot_module):
    global core
    core = bot_module
    runner = web.AppRunner(build_app(tg_app), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    runtime["web_runner"] = runner
    log.info("mini app server on http://%s:%s", HOST, PORT)
    runtime["tunnel_task"] = asyncio.create_task(watch_tunnel(tg_app))


async def stop(tg_app):
    task = runtime.pop("tunnel_task", None)
    if task:
        task.cancel()
    runner = runtime.pop("web_runner", None)
    if runner:
        await runner.cleanup()
