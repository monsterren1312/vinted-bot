"""Minimal Vinted client.

Vinted no longer exposes /api/v2/catalog/items, so listings are read from the
server-rendered catalog page (Next.js RSC payload). Brand search and seller
profiles still come from the JSON API, which needs the session cookie that the
home page sets.
"""

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import httpx

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
RSC_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
ITEMS_MARKER = '"items":{"items":'
USER_CACHE_TTL = 6 * 3600


@dataclass
class Item:
    id: int
    title: str
    price: float
    currency: str
    brand: str
    details: str
    url: str
    photo: Optional[str]
    user_id: int


@dataclass
class Seller:
    login: str
    feedback_count: int
    reputation: float


class VintedError(Exception):
    pass


class Vinted:
    def __init__(self, domain: str = "www.vinted.de"):
        self.base = f"https://{domain}"
        self.client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"},
            follow_redirects=True,
            timeout=30,
        )
        self._has_session = False
        self._users: Dict[int, Tuple[float, Seller]] = {}

    async def close(self):
        await self.client.aclose()

    async def _ensure_session(self, force: bool = False):
        if self._has_session and not force:
            return
        self.client.cookies.clear()
        r = await self.client.get(self.base + "/")
        r.raise_for_status()
        self._has_session = True

    async def _api(self, path: str, params=None) -> dict:
        await self._ensure_session()
        for attempt in range(2):
            r = await self.client.get(
                self.base + path, params=params, headers={"Accept": "application/json"}
            )
            # Limits are per session cookie, so a fresh session usually gets through.
            if r.status_code in (401, 403, 429) and attempt == 0:
                await self._ensure_session(force=True)
                continue
            if r.status_code == 429:
                raise VintedError("rate limited (429)")
            r.raise_for_status()
            return r.json()
        raise VintedError(f"{path}: {r.status_code}")

    async def search_brands(self, keyword: str) -> List[dict]:
        data = await self._api("/api/v2/brands", {"keyword": keyword})
        return [{"id": b["id"], "title": b["title"]} for b in data.get("brands", [])]

    async def seller(self, user_id: int) -> Seller:
        cached = self._users.get(user_id)
        if cached and time.time() - cached[0] < USER_CACHE_TTL:
            return cached[1]
        u = (await self._api(f"/api/v2/users/{user_id}"))["user"]
        s = Seller(
            login=u.get("login") or str(user_id),
            feedback_count=int(u.get("feedback_count") or 0),
            reputation=float(u.get("feedback_reputation") or 0),
        )
        self._users[user_id] = (time.time(), s)
        return s

    async def newest_items(
        self, brand_id: int, price_from: Optional[float], price_to: Optional[float],
        keyword: Optional[str] = None, categories: Sequence[int] = (),
        conditions: Sequence[int] = (), sizes: Sequence[int] = (), page: int = 1,
    ) -> List[Item]:
        params = [("brand_ids[]", brand_id), ("order", "newest_first")]
        if page > 1:
            params.append(("page", page))
        if price_from is not None:
            params.append(("price_from", price_from))
        if price_to is not None:
            params.append(("price_to", price_to))
        if keyword:
            params.append(("search_text", keyword))
        params += [("catalog_ids[]", c) for c in categories]
        params += [("status_ids[]", c) for c in conditions]
        params += [("size_ids[]", s) for s in sizes]
        r = await self.client.get(self.base + "/catalog", params=params)
        if r.status_code == 429:
            raise VintedError("rate limited (429)")
        r.raise_for_status()
        return [i for i in self._parse_items(r.text) if i is not None]

    def _parse_items(self, html: str) -> List[Optional[Item]]:
        text = "".join(json.loads(f'"{c}"') for c in RSC_CHUNK.findall(html))
        pos = text.find(ITEMS_MARKER)
        if pos < 0:
            raise VintedError("item list not found on catalog page (layout changed or blocked)")
        raw, _ = json.JSONDecoder().raw_decode(text, pos + len(ITEMS_MARKER))
        return [self._to_item(entry) for entry in raw]

    def _to_item(self, entry: dict) -> Optional[Item]:
        p = entry.get("productItem")
        if not p or p.get("isPromoted"):
            return None
        box = p.get("itemBox") or {}
        photo = (p.get("photos") or [{}])[0].get("url") or p.get("thumbnailUrl")
        return Item(
            id=int(p["id"]),
            title=p.get("title", ""),
            price=float(p["price"]["amount"]),
            currency=p["price"].get("currencyCode", "EUR"),
            brand=box.get("firstLine") or "",
            details=box.get("secondLine") or "",
            url=self.base + p["url"],
            photo=photo if isinstance(photo, str) else None,
            user_id=int(p["user"]["id"]),
        )
