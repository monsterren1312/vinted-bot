import asyncio
import datetime
import html
import logging
import os
import re
import sys
import time
from typing import List, Optional, Tuple

from telegram import (
    BotCommand, InlineKeyboardButton as Btn, InlineKeyboardMarkup, InputMediaPhoto,
    LinkPreviewOptions, ReplyKeyboardMarkup, Update, WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
    PicklePersistence, TypeHandler, filters,
)

import webapp
from storage import Storage
from vinted import Vinted, VintedError

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
log = logging.getLogger("bot")

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))
MIN_FEEDBACK = int(os.getenv("MIN_FEEDBACK", "1"))
VINTED_DOMAIN = os.getenv("VINTED_DOMAIN", "www.vinted.de")
DB_PATH = os.getenv("DB_PATH", "data/bot.db")
ALLOWED = {int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x}
QUIET_FROM, QUIET_TO = 23, 8  # quiet hours: listings go to the feed without a notification

POPULAR_BRANDS = [
    "Nike", "Adidas", "Jordan", "New Balance", "Carhartt", "Stone Island",
    "The North Face", "Ralph Lauren", "Levi's", "Arc'teryx", "Stüssy", "Supreme",
]
PRICE_PRESETS = [(None, None), (None, 20), (None, 50), (None, 100), (None, 200), (50, None)]
MIN_FEEDBACK_CHOICES = [0, 1, 5, 10, 50]
CATEGORIES = {  # Vinted catalog id -> label; negative keys are our own multi-catalog groups
    0: "Все категории",
    5: "👔 Мужское (всё)", 2050: "👕 Мужская одежда", 1231: "👟 Мужская обувь", 82: "🧢 Мужские аксессуары",
    1904: "👗 Женское (всё)", 4: "👚 Женская одежда", 16: "👠 Женская обувь", 1187: "💍 Женские аксессуары",
    19: "👜 Женские сумки",
    -1: "🕶 Все аксессуары и сумки", -2: "👟 Вся обувь", -3: "👕 Вся одежда",
    1193: "🧸 Детское",
}
CATEGORY_GROUPS = {-1: [82, 1187, 19], -2: [1231, 16], -3: [2050, 4]}
# Size ids are shared by all Vinted sites; only the labels differ (shoes: EU vs UK).
SIZE_GROUPS = [
    ("m", "👕 Мужская одежда", [(206, "XS"), (207, "S"), (208, "M"), (209, "L"), (210, "XL"), (211, "XXL"),
                               (212, "XXXL")]),
    ("mw", "👖 Мужские брюки", [(1636, "W28"), (1637, "W29"), (1638, "W30"), (1639, "W31"), (1640, "W32"),
                               (1641, "W33"), (1642, "W34"), (1643, "W36"), (1644, "W38")]),
    ("ms", "👟 Мужская обувь", [(778, "39", "UK 5"), (779, "39.5", "UK 5.5"), (780, "40", "UK 6"),
                               (781, "40.5", "UK 6.5"), (782, "41", "UK 7"), (783, "41.5", "UK 7.5"),
                               (784, "42", "UK 8"), (785, "42.5", "UK 8.5"), (786, "43", "UK 9"),
                               (787, "43.5", "UK 9.5"), (788, "44", "UK 10"), (789, "44.5", "UK 10.5"),
                               (790, "45", "UK 11"), (791, "45.5", "UK 11.5"), (792, "46", "UK 12"),
                               (794, "47", "UK 13")]),
    ("w", "👚 Женская одежда", [(102, "XXS"), (2, "XS"), (3, "S"), (4, "M"), (5, "L"), (6, "XL"), (7, "XXL")]),
    ("ws", "👠 Женская обувь", [(55, "35", "UK 2"), (1195, "35.5", "UK 2.5"), (56, "36", "UK 3"),
                               (1196, "36.5", "UK 3.5"), (57, "37", "UK 4"), (1197, "37.5", "UK 4.5"),
                               (58, "38", "UK 5"), (1198, "38.5", "UK 5.5"), (59, "39", "UK 6"),
                               (1199, "39.5", "UK 6.5"), (60, "40", "UK 7"), (1200, "40.5", "UK 7.5"),
                               (61, "41", "UK 8"), (1201, "41.5", "UK 8.5"), (62, "42", "UK 9")]),
]
SIZE_INDEX = {s[0]: (key, s) for key, _, sizes in SIZE_GROUPS for s in sizes}
CONDITIONS = {  # Vinted status ids
    6: "Новое с биркой", 1: "Новое без бирки", 2: "Очень хорошее", 3: "Хорошее", 4: "Удовлетворительное",
}

KB_MENU, KB_FEED, KB_ADD, KB_FILTERS, KB_FAV = "🏠 Меню", "🔥 Новые", "➕ Бренд", "📋 Фильтры", "❤️ Избранное"
REPLY_KEYBOARD = ReplyKeyboardMarkup([[KB_FEED, KB_FILTERS, KB_ADD], [KB_FAV, KB_MENU]],
                                     resize_keyboard=True, is_persistent=True)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

PRICE_RE = re.compile(r"^(\d+(?:[.,]\d+)?)?\s*-\s*(\d+(?:[.,]\d+)?)?$|^(\d+(?:[.,]\d+)?)$")
LOOSE_PRICE_RE = re.compile(r"^(до|от|up to)?\s*[€£]?\s*\d+(?:[.,]\d+)?\s*[€£]?\s*(eur|euro|евро)?$", re.I)

REGIONS = {  # domain -> (flag, label, currency symbol)
    "www.vinted.de": ("🇪🇺", "Европа · vinted.de", "€"),
    "www.vinted.co.uk": ("🇬🇧", "Англия · vinted.co.uk", "£"),
}
REGION_CODES = {"eu": ["www.vinted.de"], "uk": ["www.vinted.co.uk"], "both": ["www.vinted.de", "www.vinted.co.uk"]}
REGION_CHOICES = [("eu", "🇪🇺 Европа · vinted.de"), ("uk", "🇬🇧 Англия · vinted.co.uk"), ("both", "🇪🇺 + 🇬🇧 Оба сайта")]

HELP = (
    "❓ <b>Как пользоваться</b>\n\n"
    "1. <b>➕ Добавить бренд</b> в 3 шага: бренд (кнопкой или напиши название) → "
    "регион (🇪🇺 Европа, 🇬🇧 Англия или оба) → цена.\n"
    "2. В карточке фильтра можно уточнить поиск: 🔎 слова (например «dunk»), 👕 категория "
    "(одежда, обувь, аксессуары…), 📏 размеры, ✨ состояние вещи, 🌍 регион. "
    "Там же — 🔥 всё найденное по этому фильтру.\n"
    "3. Новые объявления приходят сообщением, а все найденные лежат в <b>🔥 Новые</b> — листай ◀️ ▶️.\n"
    "4. 🤍 — сохранить в избранное, 🚫 — больше не показывать этого продавца.\n"
    "5. 🗑 рядом с фильтром в списке удаляет его одним нажатием.\n"
    "6. <b>🕐 Объявления за период</b> — всё, что выставили за последний 1 / 3 / 6 / 24 часа "
    "(по всем фильтрам или по одному из карточки фильтра). Время примерное (≈).\n"
    "7. Старые объявления, которые продавец «поднял» наверх, не присылаю — только действительно новые.\n\n"
    "⚙️ В настройках: минимум отзывов у продавца, тихие часы "
    f"({QUIET_FROM}:00–{QUIET_TO:02d}:00 без звука, всё сохраняется в ленту), скрытые продавцы.\n\n"
    "Быстрая команда: <code>/add nike 10-50</code>"
)

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
storage = Storage(DB_PATH)
clients = {}
_locks = {}


def lock(name: str) -> asyncio.Lock:
    # Created on first use inside the running loop (on Python 3.9 a Lock binds to the loop at creation).
    if name not in _locks:
        _locks[name] = asyncio.Lock()
    return _locks[name]
state = {"last_check": None}


# --- helpers ---

def parse_price(token: str) -> Optional[Tuple[Optional[float], Optional[float]]]:
    m = PRICE_RE.match(token.replace("€", "").replace("£", "").strip())
    if not m:
        return None
    num = lambda s: float(s.replace(",", ".")) if s else None
    if m.group(3):  # a single number means "up to"
        return None, num(m.group(3))
    pf, pt = num(m.group(1)), num(m.group(2))
    if pf is not None and pt is not None and pf > pt:
        return None
    return pf, pt


def vinted(domain: str) -> Vinted:
    # One client per site: each keeps its own session cookie.
    if domain not in clients:
        clients[domain] = Vinted(domain)
    return clients[domain]


def chat_domain(chat_id: int) -> str:
    return storage.chat(chat_id)["domain"] or VINTED_DOMAIN


def region(domain: str) -> Tuple[str, str, str]:
    return REGIONS.get(domain, ("🌍", domain, "€"))


def region_code(domain: str) -> str:
    return next((c for c, ds in REGION_CODES.items() if ds == [domain]), "eu")


def code_symbol(code: str) -> str:
    return {"eu": "€", "uk": "£"}.get(code, "€/£")


def price_label(pf: Optional[float], pt: Optional[float], sym: str = "€") -> str:
    fmt = lambda v: f"{v:g} {sym}"
    if pf is not None and pt is not None:
        return f"{fmt(pf)} – {fmt(pt)}"
    if pt is not None:
        return f"до {fmt(pt)}"
    if pf is not None:
        return f"от {fmt(pf)}"
    return "любая цена"


def conditions_of(f) -> List[int]:
    return [int(c) for c in (f["conditions"] or "").split(",") if c]


def sizes_of(f) -> List[int]:
    return [int(s) for s in (f["sizes"] or "").split(",") if s]


def category_ids(category) -> List[int]:
    if not category:
        return []
    return CATEGORY_GROUPS.get(category, [category])


def size_label(size_id: int, domain: str) -> str:
    entry = SIZE_INDEX.get(size_id)
    if not entry:
        return str(size_id)
    s = entry[1]
    return s[2] if len(s) > 2 and domain == "www.vinted.co.uk" else s[1]


def sizes_label(f) -> str:
    return ", ".join(size_label(s, f["domain"]) for s in sizes_of(f))


def search_args(f) -> dict:
    return dict(keyword=f["keyword"], categories=category_ids(f["category"]),
                conditions=conditions_of(f), sizes=sizes_of(f))


def filter_extras(f) -> str:
    """Short summary of the optional search refinements, for list rows."""
    parts = []
    if f["keyword"]:
        parts.append(f"«{f['keyword']}»")
    if f["category"]:
        parts.append(CATEGORIES.get(f["category"], "?").split()[0])
    if f["conditions"]:
        parts.append("✨")
    if f["sizes"]:
        parts.append(f"📏{sizes_label(f)}")
    return " ".join(parts)


def enc(v: Optional[float]) -> str:
    return "" if v is None else f"{v:g}"


def dec(s: str) -> Optional[float]:
    return float(s) if s else None


def ago(ts: int) -> str:
    mins = int(time.time() - ts) // 60
    if mins < 1:
        return "только что"
    if mins < 60:
        return f"{mins} мин назад"
    if mins < 24 * 60:
        return f"{mins // 60} ч назад"
    return f"{mins // 1440} д назад"


def is_quiet_now() -> bool:
    h = datetime.datetime.now().hour
    return h >= QUIET_FROM or h < QUIET_TO


def min_feedback_of(chat_row) -> int:
    return MIN_FEEDBACK if chat_row["min_feedback"] is None else chat_row["min_feedback"]


def allowed(update: Update) -> bool:
    return not ALLOWED or update.effective_chat.id in ALLOWED


def caption_for(r) -> str:
    stars = f"⭐ {r['seller_reputation'] * 5:.1f}" if r["seller_feedback"] else "⭐ —"
    return (
        f"<b>{html.escape(r['title'])}</b>\n"
        f"💶 <b>{r['price']:g} {r['currency']}</b>  ·  {html.escape(r['brand'] or '')}\n"
        + (f"📏 {html.escape(r['details'])}\n" if r["details"] else "")
        + f"👤 {html.escape(r['seller_login'])} · {stars} ({r['seller_feedback']} отзывов)\n"
        f"🕒 {ago(r['found_at'])}"
    )


async def show(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, markup=None):
    """Render a text screen: edit the current menu message in place, or send a new one."""
    q = update.callback_query
    if q and q.message and q.message.text is not None:
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                      link_preview_options=NO_PREVIEW)
            return
        except BadRequest as e:
            if "not modified" in str(e):
                return
    if q and q.message:
        try:
            await q.message.delete()
        except BadRequest:
            pass
    await context.bot.send_message(update.effective_chat.id, text, parse_mode=ParseMode.HTML,
                                   reply_markup=markup, link_preview_options=NO_PREVIEW)


def price_keyboard(prefix: str, target, back: str, sym: str) -> InlineKeyboardMarkup:
    buttons = [Btn(price_label(pf, pt, sym).capitalize(), callback_data=f"{prefix}|{target}|{enc(pf)}|{enc(pt)}")
               for pf, pt in PRICE_PRESETS]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([Btn("✏️ Свой диапазон", callback_data=f"{prefix}c|{target}")])
    rows.append([Btn("« Назад", callback_data=back)])
    return InlineKeyboardMarkup(rows)


# --- screens ---

async def screen_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = storage.chat(chat_id)
    flt = storage.filters_for(chat_id)
    active = sum(1 for f in flt if f["enabled"])
    today = int(datetime.datetime.combine(datetime.date.today(), datetime.time()).timestamp())
    last = state["last_check"]
    favs = len(storage.favorites_for(chat_id))
    found_today = storage.found_count(chat_id, since=today)
    brands = ", ".join(sorted({f"{region(f['domain'])[0]} {f['brand_title']}" for f in flt if f["enabled"]}))
    text = (
        "🛍 <b>Vinted Monitor</b>\n\n"
        f"📋 Фильтров: <b>{len(flt)}</b> (активно {active})\n"
        + (f"👀 Слежу: {html.escape(brands)}\n" if brands else "")
        + f"🔥 Найдено сегодня: <b>{found_today}</b>\n"
        f"👤 Продавцы от {min_feedback_of(chat)} отзывов\n"
        + ("⏸ <b>Уведомления на паузе</b>\n" if chat["paused"] else
           f"▶️ Проверяю каждые {max(CHECK_INTERVAL // 60, 1)} мин\n")
        + (f"🌙 Тихие часы {QUIET_FROM}:00–{QUIET_TO:02d}:00\n" if chat["quiet"] else "")
        + (f"🕒 Последняя проверка: {last.strftime('%H:%M')}" if last else "")
    )
    markup = InlineKeyboardMarkup([
        *([[Btn("📱 Открыть приложение", web_app=WebAppInfo(webapp.public_url["value"]))]]
          if webapp.public_url["value"] else []),
        [Btn("➕ Добавить бренд", callback_data="add")],
        [Btn(f"📋 Мои фильтры ({len(flt)})", callback_data="fl"),
         Btn(f"🔥 Новые ({found_today})", callback_data="it|feed|0")],
        [Btn(f"❤️ Избранное ({favs})", callback_data="it|fav|0"), Btn("🔄 Проверить сейчас", callback_data="now")],
        [Btn("▶️ Включить уведомления" if chat["paused"] else "⏸ Пауза", callback_data="pause"),
         Btn("⚙️ Настройки", callback_data="st")],
        [Btn("🕐 Объявления за период", callback_data="pm|all")],
        [Btn("❓ Помощь", callback_data="help")],
    ])
    await show(update, context, text, markup)


async def screen_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["await"] = ("brand",)
    buttons = [Btn(name, callback_data=f"pb|{i}") for i, name in enumerate(POPULAR_BRANDS)]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([Btn("« Меню", callback_data="m")])
    await show(update, context,
               "➕ <b>Новый фильтр</b> · шаг 1 из 3: бренд\n\n"
               "Выбери популярный бренд или просто <b>напиши название</b> в чат 👇",
               InlineKeyboardMarkup(rows))


async def brand_search(update: Update, context: ContextTypes.DEFAULT_TYPE, keyword: str):
    try:
        brands = (await vinted(chat_domain(update.effective_chat.id)).search_brands(keyword))[:8]
    except Exception as e:
        log.warning("brand search failed: %s", e)
        await show(update, context, "😕 Vinted сейчас не отвечает, попробуй через минуту.",
                   InlineKeyboardMarkup([[Btn("« Меню", callback_data="m")]]))
        return
    if not brands:
        context.user_data["await"] = ("brand",)
        await show(update, context, f"Бренд «{html.escape(keyword)}» не найден. Напиши по-другому 👇",
                   InlineKeyboardMarkup([[Btn("« Назад", callback_data="add")]]))
        return
    context.chat_data.setdefault("brands", {}).update({b["id"]: b["title"] for b in brands})
    exact = [b for b in brands if b["title"].lower() == keyword.lower()]
    if exact:
        await brand_chosen(update, context, exact[0]["id"])
        return
    rows = [[Btn(b["title"], callback_data=f"b|{b['id']}")] for b in brands]
    rows.append([Btn("« Назад", callback_data="add")])
    await show(update, context, "Какой именно бренд?", InlineKeyboardMarkup(rows))


async def brand_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE, brand_id: int):
    pending = context.chat_data.pop("pending_price", None)
    if pending is not None:
        await create_filter(update, context, brand_id, region_code(chat_domain(update.effective_chat.id)), *pending)
        return
    title = context.chat_data.get("brands", {}).get(brand_id, f"#{brand_id}")
    last = context.chat_data.get("last_region") or region_code(chat_domain(update.effective_chat.id))
    rows = [[Btn(("✅ " if code == last else "") + label, callback_data=f"nr|{brand_id}|{code}")]
            for code, label in REGION_CHOICES]
    rows.append([Btn("« Назад", callback_data="add")])
    await show(update, context,
               f"➕ <b>{html.escape(title)}</b> · шаг 2 из 3: регион\n\n🌍 На каком сайте искать?",
               InlineKeyboardMarkup(rows))


async def region_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE, brand_id: int, code: str):
    context.chat_data["last_region"] = code
    title = context.chat_data.get("brands", {}).get(brand_id, f"#{brand_id}")
    flags = " + ".join(region(d)[0] for d in REGION_CODES[code])
    await show(update, context,
               f"➕ <b>{html.escape(title)}</b> {flags} · шаг 3 из 3: цена\n\n💶 Какая цена?",
               price_keyboard("np", f"{brand_id}|{code}", f"b|{brand_id}", code_symbol(code)))


async def create_filter(update, context, brand_id: int, code: str, pf, pt):
    chat_id = update.effective_chat.id
    title = context.chat_data.get("brands", {}).get(brand_id, f"#{brand_id}")
    domains = REGION_CODES.get(code, [VINTED_DOMAIN])
    ids = [storage.add_filter(chat_id, brand_id, title, pf, pt, d) for d in domains]
    storage.set_chat_domain(chat_id, domains[0])
    if len(ids) > 1:
        await screen_filters(update, context,
                             note=f"✅ Добавлено: <b>{html.escape(title)}</b> на обоих сайтах 🇪🇺 🇬🇧\n"
                                  "Открой фильтр, чтобы уточнить поиск.\n\n")
        return
    await screen_filter(update, context, ids[0],
                        note="✅ Фильтр добавлен! Пришлю объявления, которые появятся с этого момента.\n"
                             "Можно уточнить поиск кнопками ниже 👇\n\n")


async def screen_filters(update: Update, context: ContextTypes.DEFAULT_TYPE, note: str = ""):
    flt = storage.filters_for(update.effective_chat.id)
    rows = []
    for f in flt:
        icon = "🟢" if f["enabled"] else "⚪️"
        flag, _, sym = region(f["domain"])
        label = f"{icon} {flag} {f['brand_title']} · {price_label(f['price_from'], f['price_to'], sym)}"
        extras = filter_extras(f)
        rows.append([Btn(f"{label} {extras}".strip(), callback_data=f"f|{f['id']}"),
                     Btn("🗑", callback_data=f"fx|{f['id']}")])
    rows.append([Btn("➕ Добавить бренд", callback_data="add")])
    if flt:
        any_on = any(f["enabled"] for f in flt)
        rows.append([Btn("⚪️ Выключить все" if any_on else "🟢 Включить все", callback_data=f"fa|{0 if any_on else 1}"),
                     Btn("🧹 Удалить все", callback_data="fxa")])
    rows.append([Btn("« Меню", callback_data="m")])
    text = (note + "📋 <b>Мои фильтры</b>\n\nНажми на фильтр, чтобы настроить его, или 🗑 — чтобы удалить."
            if flt else note + "📋 Фильтров пока нет. Добавь первый бренд 👇")
    await show(update, context, text, InlineKeyboardMarkup(rows))


async def screen_filter(update: Update, context: ContextTypes.DEFAULT_TYPE, fid: int, note: str = ""):
    chat_id = update.effective_chat.id
    f = storage.get_filter(chat_id, fid)
    if not f:
        await screen_filters(update, context)
        return
    flag, region_name, sym = region(f["domain"])
    conds = conditions_of(f)
    text = (
        f"{note}🏷 <b>{html.escape(f['brand_title'])}</b>\n\n"
        f"{flag} Регион: {region_name}\n"
        f"💶 Цена: {price_label(f['price_from'], f['price_to'], sym)}\n"
        f"🔎 Слова: {html.escape(f['keyword']) if f['keyword'] else 'любые'}\n"
        f"👕 Категория: {CATEGORIES.get(f['category'] or 0, 'Все категории')}\n"
        f"📏 Размер: {html.escape(sizes_label(f)) if f['sizes'] else 'любой'}\n"
        f"✨ Состояние: {', '.join(CONDITIONS[c] for c in conds if c in CONDITIONS) if conds else 'любое'}\n"
        f"{'🟢 Активен' if f['enabled'] else '⚪️ Выключен'} · 🔥 Найдено: {storage.found_count(chat_id, fid)}"
    )
    found = storage.found_count(chat_id, fid)
    markup = InlineKeyboardMarkup([
        [Btn("💶 Цена", callback_data=f"fp|{fid}"), Btn(f"{flag} Регион", callback_data=f"frg|{fid}"),
         Btn("🔎 Слова", callback_data=f"kw|{fid}")],
        [Btn("👕 Категория", callback_data=f"ct|{fid}"), Btn("📏 Размер", callback_data=f"szg|{fid}"),
         Btn("✨ Состояние", callback_data=f"cd|{fid}")],
        [Btn(f"🔥 Найденное ({found})", callback_data=f"it|flt:{fid}|0"),
         Btn("🕐 За период", callback_data=f"pm|{fid}")],
        [Btn("⚪️ Выключить" if f["enabled"] else "🟢 Включить", callback_data=f"ft|{fid}"),
         Btn("🗑 Удалить", callback_data=f"fd|{fid}")],
        [Btn("➕ Ещё бренд", callback_data="add"), Btn("« Фильтры", callback_data="fl"),
         Btn("🏠 Меню", callback_data="m")],
    ])
    await show(update, context, text, markup)


async def screen_category(update: Update, context: ContextTypes.DEFAULT_TYPE, fid: int):
    f = storage.get_filter(update.effective_chat.id, fid)
    if not f:
        await screen_filters(update, context)
        return
    current = f["category"] or 0
    buttons = [Btn(("✅ " if cid == current else "") + name, callback_data=f"cs|{fid}|{cid}")
               for cid, name in CATEGORIES.items()]
    rows = [buttons[:1]] + [buttons[i:i + 2] for i in range(1, len(buttons), 2)]
    rows.append([Btn("« Назад", callback_data=f"f|{fid}")])
    await show(update, context, f"👕 <b>{html.escape(f['brand_title'])}</b> — какая категория?",
               InlineKeyboardMarkup(rows))


async def screen_size_groups(update: Update, context: ContextTypes.DEFAULT_TYPE, fid: int):
    f = storage.get_filter(update.effective_chat.id, fid)
    if not f:
        await screen_filters(update, context)
        return
    selected = set(sizes_of(f))
    rows = []
    for key, label, sizes in SIZE_GROUPS:
        n = sum(1 for s in sizes if s[0] in selected)
        rows.append([Btn(f"{label}" + (f" · ✅ {n}" if n else ""), callback_data=f"szo|{fid}|{key}")])
    rows.append([Btn("Любой размер", callback_data=f"szr|{fid}"), Btn("✔️ Готово", callback_data=f"f|{fid}")])
    await show(update, context,
               f"📏 <b>{html.escape(f['brand_title'])}</b> — размеры\n\n"
               f"Выбрано: {html.escape(sizes_label(f)) if selected else 'любой размер'}\n\n"
               "Выбери тип вещей, потом отметь размеры. Можно сочетать, например одежду M и обувь 43.",
               InlineKeyboardMarkup(rows))


async def screen_sizes(update: Update, context: ContextTypes.DEFAULT_TYPE, fid: int, group: str):
    f = storage.get_filter(update.effective_chat.id, fid)
    grp = next((g for g in SIZE_GROUPS if g[0] == group), None)
    if not f or not grp:
        await screen_filters(update, context)
        return
    selected = set(sizes_of(f))
    buttons = [Btn(("✅ " if s[0] in selected else "") + size_label(s[0], f["domain"]),
                   callback_data=f"szt|{fid}|{group}|{s[0]}") for s in grp[2]]
    per_row = 4 if len(buttons) > 9 else 3
    rows = [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]
    rows.append([Btn("« Типы размеров", callback_data=f"szg|{fid}"), Btn("✔️ Готово", callback_data=f"f|{fid}")])
    await show(update, context, f"📏 <b>{html.escape(f['brand_title'])}</b> — {grp[1]}\n\nОтметь нужные размеры:",
               InlineKeyboardMarkup(rows))


async def screen_conditions(update: Update, context: ContextTypes.DEFAULT_TYPE, fid: int):
    f = storage.get_filter(update.effective_chat.id, fid)
    if not f:
        await screen_filters(update, context)
        return
    selected = conditions_of(f)
    rows = [[Btn(("✅ " if sid in selected else "▫️ ") + name, callback_data=f"cdt|{fid}|{sid}")]
            for sid, name in CONDITIONS.items()]
    rows.append([Btn("Любое состояние", callback_data=f"cdr|{fid}"), Btn("✔️ Готово", callback_data=f"f|{fid}")])
    await show(update, context,
               f"✨ <b>{html.escape(f['brand_title'])}</b> — какое состояние?\n\n"
               "Отметь один или несколько вариантов. Ничего не отмечено = любое.",
               InlineKeyboardMarkup(rows))


async def screen_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat = storage.chat(chat_id)
    current = min_feedback_of(chat)
    buttons = [Btn(("✅ " if n == current else "") + str(n), callback_data=f"mf|{n}") for n in MIN_FEEDBACK_CHOICES]
    blocked = len(storage.blocked_for(chat_id))
    await show(update, context,
               "⚙️ <b>Настройки</b>\n\n"
               "👤 <b>Минимум отзывов у продавца</b> — продавцов с меньшим числом отзывов "
               "(новые аккаунты) пропускаю.\n\n"
               f"🌙 <b>Тихие часы</b> {QUIET_FROM}:00–{QUIET_TO:02d}:00 — ночью не присылаю уведомления, "
               "но всё найденное сохраняется в 🔥 Новые.",
               InlineKeyboardMarkup([
                   buttons,
                   [Btn(f"🌙 Тихие часы: {'вкл' if chat['quiet'] else 'выкл'}", callback_data="qt")],
                   [Btn(f"🚫 Скрытые продавцы ({blocked})", callback_data="bls")],
                   [Btn("🧹 Очистить ленту найденного", callback_data="clr")],
                   [Btn("« Меню", callback_data="m")],
               ]))


async def screen_blocked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = [[Btn(f"✖️ {b['seller_login']}", callback_data=f"ub|{b['seller_id']}")]
            for b in storage.blocked_for(update.effective_chat.id)]
    rows.append([Btn("« Настройки", callback_data="st")])
    text = ("🚫 <b>Скрытые продавцы</b>\n\nНажми на продавца, чтобы снова показывать его объявления."
            if len(rows) > 1 else "🚫 Скрытых продавцов нет.\n\nСкрыть продавца можно кнопкой 🚫 под объявлением.")
    await show(update, context, text, InlineKeyboardMarkup(rows))


async def screen_filter_region(update: Update, context: ContextTypes.DEFAULT_TYPE, fid: int):
    f = storage.get_filter(update.effective_chat.id, fid)
    if not f:
        await screen_filters(update, context)
        return
    rows = [[Btn(("✅ " if d == f["domain"] else "") + f"{flag} {name}",
                 callback_data=f"frs|{fid}|{region_code(d)}")]
            for d, (flag, name, _) in REGIONS.items()]
    rows.append([Btn("« Назад", callback_data=f"f|{fid}")])
    await show(update, context,
               f"🌍 <b>{html.escape(f['brand_title'])}</b> — на каком сайте искать?\n\n"
               "Чтобы следить за брендом на обоих сайтах, добавь его ещё раз через ➕ и выбери «Оба сайта».",
               InlineKeyboardMarkup(rows))


async def screen_items(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, idx: int):
    """Swipeable listing viewer: kind is "feed", "fav" or "flt:<filter id>"."""
    chat_id = update.effective_chat.id
    fid = int(kind[4:]) if kind.startswith("flt:") else None
    if kind == "fav":
        rows = storage.favorites_for(chat_id)
    else:
        rows = storage.found_for(chat_id, fid)
    if fid is not None:
        bottom = [Btn("« К фильтру", callback_data=f"f|{fid}"), Btn("🏠 Меню", callback_data="m")]
    elif kind == "fav":
        bottom = [Btn("🔥 Новые", callback_data="it|feed|0"), Btn("🏠 Меню", callback_data="m")]
    else:
        bottom = [Btn("🔄 Обновить", callback_data="it|feed|0"), Btn("🏠 Меню", callback_data="m")]
    if not rows:
        text = {"feed": "🔥 Пока ничего не найдено.\n\nНовые объявления появятся здесь, как только бот их найдёт.",
                "fav": "❤️ В избранном пусто.\n\nНажимай 🤍 под объявлениями, чтобы сохранить их."
                }.get(kind, "🔥 По этому фильтру пока ничего не найдено.")
        await show(update, context, text, InlineKeyboardMarkup(
            [[Btn("🔄 Проверить сейчас", callback_data="now")], bottom]))
        return
    idx = max(0, min(idx, len(rows) - 1))
    r = rows[idx]
    n = len(rows)
    item_id = r["item_id"]
    fav = kind == "fav" or storage.is_favorite(chat_id, item_id)
    actions = [Btn("💔 Убрать" if fav else "🤍 В избранное", callback_data=f"ff|{kind}|{idx}|{item_id}")]
    if kind != "fav" and r["seller_id"]:
        actions.append(Btn("🚫 Скрыть продавца", callback_data=f"fb|{kind}|{idx}|{item_id}"))
    markup = InlineKeyboardMarkup([
        [Btn("⏮" if idx > 1 else "·", callback_data=f"it|{kind}|0" if idx > 1 else "noop"),
         Btn("◀️", callback_data=f"it|{kind}|{idx - 1}" if idx > 0 else "noop"),
         Btn(f"{idx + 1} / {n}", callback_data="noop"),
         Btn("▶️", callback_data=f"it|{kind}|{idx + 1}" if idx < n - 1 else "noop")],
        [Btn("🛒 Открыть на Vinted", url=r["url"])],
        actions,
        bottom,
    ])
    flag = region(r["url"].split("/")[2])[0] if kind != "fav" else "❤️"
    await render_card(update, context, f"{flag} " + caption_for(r), r["photo"], markup)


async def render_card(update: Update, context: ContextTypes.DEFAULT_TYPE, caption: str, photo, markup):
    """Show a listing card: swap the photo in place when possible, otherwise send a new message."""
    q = update.callback_query
    if q and q.message and q.message.photo and photo:
        try:
            await q.edit_message_media(InputMediaPhoto(photo, caption=caption, parse_mode=ParseMode.HTML),
                                       reply_markup=markup)
            return
        except BadRequest as e:
            if "not modified" in str(e):
                return
            log.info("edit media failed, resending: %s", e)
    if q and q.message:
        try:
            await q.message.delete()
        except BadRequest:
            pass
    chat_id = update.effective_chat.id
    if photo:
        try:
            await context.bot.send_photo(chat_id, photo, caption=caption,
                                         parse_mode=ParseMode.HTML, reply_markup=markup)
            return
        except BadRequest as e:
            log.info("photo send failed: %s", e)
    await context.bot.send_message(chat_id, caption, parse_mode=ParseMode.HTML, reply_markup=markup)


# --- listings for a period ---

FALLBACK_IDS_PER_SEC = 125  # ~450k new listings/hour across Vinted; used until the clock has data
PERIOD_MAX_PAGES = 4  # up to ~380 listings per filter
PERIODS = [(1, "1 час"), (3, "3 часа"), (6, "6 часов"), (24, "24 часа")]


def _clock_rate(pts) -> float:
    if len(pts) >= 2 and pts[-1][0] - pts[0][0] >= 1800:
        return (pts[-1][1] - pts[0][1]) / (pts[-1][0] - pts[0][0])
    return FALLBACK_IDS_PER_SEC


def id_at(ts: float) -> int:
    """Estimate the newest listing id at a moment in time (ids grow with posting time)."""
    pts = storage.clock()
    if not pts:
        return 0
    for (t0, i0), (t1, i1) in zip(pts, pts[1:]):
        if t0 <= ts <= t1:
            return int(i0 + (ts - t0) * (i1 - i0) / max(t1 - t0, 1))
    if ts < pts[0][0]:
        return int(pts[0][1] - (pts[0][0] - ts) * _clock_rate(pts))
    return int(pts[-1][1] + (ts - pts[-1][0]) * _clock_rate(pts))


def posted_at(item_id: int) -> Optional[float]:
    pts = storage.clock()
    if not pts:
        return None
    for (t0, i0), (t1, i1) in zip(pts, pts[1:]):
        if i0 <= item_id <= i1:
            return t0 + (item_id - i0) * (t1 - t0) / max(i1 - i0, 1)
    if item_id < pts[0][1]:
        return pts[0][0] - (pts[0][1] - item_id) / _clock_rate(pts)
    return min(time.time(), pts[-1][0] + (item_id - pts[-1][1]) / _clock_rate(pts))


def period_scope_label(chat_id: int, scope: str) -> str:
    if scope == "all":
        return "все активные фильтры"
    f = storage.get_filter(chat_id, int(scope))
    return f"{region(f['domain'])[0]} {f['brand_title']}" if f else "фильтр"


async def screen_period_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, scope: str):
    chat_id = update.effective_chat.id
    buttons = [Btn(f"⏱ {label}", callback_data=f"per|{scope}|{h}") for h, label in PERIODS]
    back = Btn("« К фильтру", callback_data=f"f|{scope}") if scope != "all" else Btn("« Меню", callback_data="m")
    await show(update, context,
               "🕐 <b>Объявления за период</b>\n\n"
               f"По: <b>{html.escape(period_scope_label(chat_id, scope))}</b>\n\n"
               "Найду на Vinted всё, что выставили за выбранное время — даже то, что появилось "
               "до добавления фильтра. Продавцов без отзывов пропускаю.",
               InlineKeyboardMarkup([buttons[:2], buttons[2:], [back]]))


async def period_search(chat_id: int, scope: str, hours: int) -> List[dict]:
    flt = storage.active_filters(chat_id) if scope == "all" else [
        f for f in [storage.get_filter(chat_id, int(scope))] if f]
    since_id = None
    found = {}
    async with lock("check"):
        for f in flt:
            client = vinted(f["domain"])
            for page in range(1, PERIOD_MAX_PAGES + 1):
                try:
                    items = await client.newest_items(f["brand_id"], f["price_from"], f["price_to"],
                                                      page=page, **search_args(f))
                except Exception as e:
                    log.warning("period search %s p%s: %s", f["id"], page, e)
                    break
                record_clock(items)
                if since_id is None:  # after the first page, so the clock has at least one sample
                    since_id = id_at(time.time() - hours * 3600)
                fresh = [i for i in items if i.id >= since_id and not storage.is_blocked(chat_id, i.user_id)]
                for i in fresh:
                    found[i.id] = {
                        "item_id": i.id, "title": i.title, "price": i.price, "currency": i.currency,
                        "brand": i.brand, "details": i.details, "url": i.url, "photo": i.photo,
                        "user_id": i.user_id, "domain": f["domain"],
                    }
                # "Newest first" mixes in bumped old listings, so stop on a page with nothing fresh.
                if not fresh or len(items) < 90:
                    break
                await asyncio.sleep(1.5)
            await asyncio.sleep(1.5)
    return sorted(found.values(), key=lambda d: -d["item_id"])


async def screen_period_item(update: Update, context: ContextTypes.DEFAULT_TYPE, idx: int):
    chat_id = update.effective_chat.id
    period = context.chat_data.get("period")
    if not period:
        await screen_period_menu(update, context, "all")
        return
    items = period["items"]
    min_fb = min_feedback_of(storage.chat(chat_id))
    # Sellers are checked lazily, one card at a time, to stay under Vinted's rate limits.
    for _ in range(8):
        if not items:
            break
        idx = max(0, min(idx, len(items) - 1))
        d = items[idx]
        if "seller_login" in d:
            break
        try:
            s = await vinted(d["domain"]).seller(d["user_id"])
        except Exception as e:
            log.info("period seller lookup failed: %s", e)
            d.update(seller_login="?", seller_feedback=0, seller_reputation=0.0, seller_unknown=True)
            break
        if s.feedback_count < min_fb:
            items.pop(idx)
            continue
        d.update(seller_login=s.login, seller_feedback=s.feedback_count, seller_reputation=s.reputation)
    back = Btn("🕐 Другой период", callback_data=f"pm|{period['scope']}")
    if not items:
        await show(update, context,
                   f"🕐 За {period['label']} подходящих объявлений не нашлось.",
                   InlineKeyboardMarkup([[back], [Btn("🏠 Меню", callback_data="m")]]))
        return
    idx = max(0, min(idx, len(items) - 1))
    d = items[idx]
    n = len(items)
    posted = posted_at(d["item_id"])
    d["found_at"] = posted or time.time()
    caption = (
        f"{region(d['domain'])[0]} <b>{html.escape(d['title'])}</b>\n"
        f"💶 <b>{d['price']:g} {d['currency']}</b>  ·  {html.escape(d['brand'] or '')}\n"
        + (f"📏 {html.escape(d['details'])}\n" if d["details"] else "")
        + (f"👤 {html.escape(d['seller_login'])} · ⭐ {d['seller_reputation'] * 5:.1f} "
           f"({d['seller_feedback']} отзывов)\n" if not d.get("seller_unknown") else "👤 продавец не проверен\n")
        + (f"🕒 ≈ {ago(posted)}" if posted else "")
        + f"\n\n🕐 За {period['label']} · {html.escape(period['scope_label'])}"
    )
    fav = storage.is_favorite(chat_id, d["item_id"])
    markup = InlineKeyboardMarkup([
        [Btn("⏮" if idx > 1 else "·", callback_data="pv|0" if idx > 1 else "noop"),
         Btn("◀️", callback_data=f"pv|{idx - 1}" if idx > 0 else "noop"),
         Btn(f"{idx + 1} / {n}", callback_data="noop"),
         Btn("▶️", callback_data=f"pv|{idx + 1}" if idx < n - 1 else "noop")],
        [Btn("🛒 Открыть на Vinted", url=d["url"])],
        [Btn("❤️ Сохранено" if fav else "🤍 В избранное", callback_data=f"pfv|{idx}")],
        [back, Btn("🏠 Меню", callback_data="m")],
    ])
    await render_card(update, context, caption, d["photo"], markup)


def record_clock(items):
    if items:
        storage.add_clock(int(time.time()), max(i.id for i in items))


def notification_markup(chat_id: int, r) -> InlineKeyboardMarkup:
    fav = storage.is_favorite(chat_id, r["item_id"])
    return InlineKeyboardMarkup([
        [Btn("🛒 Открыть на Vinted", url=r["url"])],
        [Btn("❤️ Сохранено" if fav else "🤍 В избранное", callback_data=f"nf|{r['item_id']}"),
         Btn("🚫 Продавец", callback_data=f"nb|{r['item_id']}")],
    ])


# --- handlers ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        await update.message.reply_text(f"Нет доступа. Ваш chat id: {update.effective_chat.id}")
        return
    await update.message.reply_text(
        "👋 Привет! Я слежу за новыми объявлениями на Vinted и присылаю их сюда.\n"
        "Кнопки внизу всегда под рукой 👇", reply_markup=REPLY_KEYBOARD)
    await screen_main(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        await show(update, context, HELP, InlineKeyboardMarkup([[Btn("🏠 Меню", callback_data="m")]]))


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log what users press or type (diagnostics); runs before the real handlers."""
    if update.callback_query:
        log.info("button %r from %s", update.callback_query.data, update.effective_chat.id)
    elif update.message and update.message.text:
        log.info("message %r from %s", update.message.text[:40], update.effective_chat.id)
    elif update.message and update.message.web_app_data:
        log.info("web_app_data from %s", update.effective_chat.id)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("handler error: %s", context.error, exc_info=context.error)


async def cmd_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    url = webapp.public_url["value"]
    if not url:
        await update.message.reply_text("📱 Приложение сейчас запускается — попробуй через минуту.")
        return
    await update.message.reply_text(
        "📱 Всё управление в одном окне: лента, фильтры, период, избранное, настройки.",
        reply_markup=InlineKeyboardMarkup([[Btn("📱 Открыть приложение", web_app=WebAppInfo(url))]]))


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        await screen_main(update, context)


async def cmd_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        await screen_items(update, context, "feed", 0)


async def cmd_fav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        await screen_items(update, context, "fav", 0)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        await screen_filters(update, context)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/add without args opens the menu; `/add nike 10-50` still works as a shortcut."""
    if not allowed(update):
        return
    args = list(context.args)
    if not args:
        await screen_add(update, context)
        return
    if len(args) > 1 and parse_price(args[-1]) is not None:
        context.chat_data["pending_price"] = parse_price(args.pop())
    await brand_search(update, context, " ".join(args))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    text = update.message.text.strip()
    shortcuts = {
        KB_MENU: lambda: screen_main(update, context),
        KB_FEED: lambda: screen_items(update, context, "feed", 0),
        KB_FAV: lambda: screen_items(update, context, "fav", 0),
        KB_FILTERS: lambda: screen_filters(update, context),
        KB_ADD: lambda: screen_add(update, context),
    }
    if text in shortcuts:
        context.user_data.pop("await", None)
        await shortcuts[text]()
        return

    chat_id = update.effective_chat.id
    waiting = context.user_data.pop("await", None)
    if waiting and waiting[0] in ("npc", "epc"):
        price = parse_price(text)
        if price is None:
            context.user_data["await"] = waiting
            await update.message.reply_text("Не понял цену 🤔 Напиши например <code>10-50</code>, "
                                            "<code>-100</code> или <code>20-</code>", parse_mode=ParseMode.HTML)
            return
        if waiting[0] == "npc":
            await create_filter(update, context, waiting[1], waiting[2], *price)
        else:
            storage.set_price(chat_id, waiting[1], *price)
            await screen_filter(update, context, waiting[1], note="✅ Цена обновлена.\n\n")
        return
    if waiting and waiting[0] == "kw":
        storage.set_option(chat_id, waiting[1], "keyword", text[:60])
        await screen_filter(update, context, waiting[1], note="✅ Слова поиска сохранены.\n\n")
        return
    if parse_price(text) is not None or LOOSE_PRICE_RE.match(text):
        # A bare price with no price prompt open: don't turn "200" into a brand search.
        await show(update, context,
                   "💶 Похоже на цену. Чтобы задать цену, открой фильтр и нажми «💶 Цена», "
                   "или добавь новый бренд.",
                   InlineKeyboardMarkup([[Btn("📋 Мои фильтры", callback_data="fl"),
                                          Btn("➕ Добавить бренд", callback_data="add")]]))
        return
    # Anything else typed into the chat is treated as a brand name.
    await brand_search(update, context, text)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not allowed(update):
        await q.answer()
        return
    chat_id = update.effective_chat.id
    action, *args = q.data.split("|")

    # Actions on a notification keep the notification itself in the chat.
    if action == "nf":
        r = storage.get_found(chat_id, int(args[0]))
        if not r:
            await q.answer("Объявление уже удалено из ленты")
            return
        saved = storage.toggle_favorite(chat_id, r["item_id"])
        await q.answer("❤️ Сохранено в избранное" if saved else "Убрано из избранного")
        try:
            await q.edit_message_reply_markup(notification_markup(chat_id, r))
        except BadRequest:
            pass
        return
    if action == "nb":
        r = storage.get_found(chat_id, int(args[0]))
        if not r or not r["seller_id"]:
            await q.answer("Не получилось — открой объявление в ленте 🔥")
            return
        storage.block_seller(chat_id, r["seller_id"], r["seller_login"])
        await q.answer(f"🚫 Продавец {r['seller_login']} скрыт", show_alert=True)
        try:
            await q.message.delete()
        except BadRequest:
            pass
        return
    if action == "noop":
        await q.answer()
        return
    if action == "now":
        await q.answer("Проверяю…")
        found = await run_checks(context, chat_id)
        if found:
            await screen_items(update, context, "feed", 0)
        else:
            await context.bot.send_message(chat_id, "🔄 Проверил — новых объявлений пока нет.")
        return
    if action == "per":
        scope, hours = args[0], int(args[1])
        label = dict(PERIODS).get(hours, f"{hours} ч")
        scope_label = period_scope_label(chat_id, scope)
        await q.answer("Ищу…")
        await show(update, context, f"⏳ Ищу объявления за последние {label}…\nПо: {html.escape(scope_label)}\n\n"
                                    "Это может занять до минуты.")
        items = await period_search(chat_id, scope, hours)
        context.chat_data["period"] = {"items": items, "label": label, "scope": scope, "scope_label": scope_label}
        await screen_period_item(update, context, 0)
        return
    if action == "pfv":
        period = context.chat_data.get("period") or {"items": []}
        idx = int(args[0])
        if idx < len(period["items"]):
            d = period["items"][idx]
            if storage.is_favorite(chat_id, d["item_id"]):
                storage.db.execute("DELETE FROM favorites WHERE chat_id = ? AND item_id = ?", (chat_id, d["item_id"]))
                storage.db.commit()
                await q.answer("Убрано из избранного")
            else:
                storage.save_favorite(chat_id, {**d, "found_at": d.get("found_at") or time.time()})
                await q.answer("❤️ Сохранено в избранное")
        else:
            await q.answer()
        await screen_period_item(update, context, idx)
        return
    if action == "fx":
        f = storage.get_filter(chat_id, int(args[0]))
        storage.delete_filter(chat_id, int(args[0]))
        await q.answer(f"🗑 {f['brand_title']} удалён" if f else "Удалено")
        await screen_filters(update, context)
        return
    if action == "ff":
        kind, idx, item_id = args[0], int(args[1]), int(args[2])
        saved = storage.toggle_favorite(chat_id, item_id)
        await q.answer("❤️ Сохранено в избранное" if saved else "Убрано из избранного")
        if kind == "fav" and not saved:
            idx = max(idx - 1, 0) if idx >= len(storage.favorites_for(chat_id)) else idx
        await screen_items(update, context, kind, idx)
        return
    if action == "fb":
        if len(args) == 2:  # buttons from before the per-filter feed existed
            args = ["feed"] + args
        kind, idx, item_id = args[0], int(args[1]), int(args[2])
        r = storage.get_found(chat_id, item_id)
        if r and r["seller_id"]:
            storage.block_seller(chat_id, r["seller_id"], r["seller_login"])
            await q.answer(f"🚫 Продавец {r['seller_login']} скрыт")
        else:
            await q.answer()
        await screen_items(update, context, kind, idx)
        return

    await q.answer()
    if action != "add":
        context.user_data.pop("await", None)

    if action == "m":
        await screen_main(update, context)
    elif action == "help":
        await show(update, context, HELP, InlineKeyboardMarkup([[Btn("« Меню", callback_data="m")]]))
    elif action == "add":
        await screen_add(update, context)
    elif action == "pb":
        await brand_search(update, context, POPULAR_BRANDS[int(args[0])])
    elif action == "b":
        await brand_chosen(update, context, int(args[0]))
    elif action == "nr":
        await region_chosen(update, context, int(args[0]), args[1])
    elif action == "np":
        await create_filter(update, context, int(args[0]), args[1], dec(args[2]), dec(args[3]))
    elif action in ("npc", "epc"):
        if action == "npc":
            context.user_data["await"] = ("npc", int(args[0]), args[1])
            back = f"nr|{args[0]}|{args[1]}"
        else:
            context.user_data["await"] = ("epc", int(args[0]))
            back = f"f|{args[0]}"
        await show(update, context,
                   "✏️ Напиши диапазон цены в чат:\n\n"
                   "<code>10-50</code> — от 10 до 50\n<code>-100</code> — до 100\n<code>20-</code> — от 20",
                   InlineKeyboardMarkup([[Btn("« Назад", callback_data=back)]]))
    elif action == "fl":
        await screen_filters(update, context)
    elif action == "f":
        await screen_filter(update, context, int(args[0]))
    elif action == "fp":
        f = storage.get_filter(chat_id, int(args[0]))
        if f:
            await show(update, context, f"<b>{html.escape(f['brand_title'])}</b>\n\n💶 Новая цена:",
                       price_keyboard("ep", f["id"], f"f|{f['id']}", region(f["domain"])[2]))
    elif action == "ep":
        storage.set_price(chat_id, int(args[0]), dec(args[1]), dec(args[2]))
        await screen_filter(update, context, int(args[0]), note="✅ Цена обновлена.\n\n")
    elif action == "kw":
        f = storage.get_filter(chat_id, int(args[0]))
        if f:
            context.user_data["await"] = ("kw", f["id"])
            rows = [[Btn("🧹 Убрать слова", callback_data=f"kwr|{f['id']}")]] if f["keyword"] else []
            rows.append([Btn("« Назад", callback_data=f"f|{f['id']}")])
            await show(update, context,
                       f"🔎 <b>{html.escape(f['brand_title'])}</b> — напиши в чат, что искать в названии.\n\n"
                       "Например: <code>dunk</code>, <code>air max 90</code>, <code>hoodie</code>",
                       InlineKeyboardMarkup(rows))
    elif action == "kwr":
        storage.set_option(chat_id, int(args[0]), "keyword", None)
        await screen_filter(update, context, int(args[0]), note="✅ Слова убраны.\n\n")
    elif action == "ct":
        await screen_category(update, context, int(args[0]))
    elif action == "cs":
        storage.set_option(chat_id, int(args[0]), "category", int(args[1]) or None)
        await screen_filter(update, context, int(args[0]), note="✅ Категория сохранена.\n\n")
    elif action == "szg":
        await screen_size_groups(update, context, int(args[0]))
    elif action == "szo":
        await screen_sizes(update, context, int(args[0]), args[1])
    elif action == "szt":
        f = storage.get_filter(chat_id, int(args[0]))
        if f:
            sid = int(args[2])
            selected = sizes_of(f)
            selected = [s for s in selected if s != sid] if sid in selected else selected + [sid]
            storage.set_option(chat_id, f["id"], "sizes", ",".join(map(str, selected)) or None)
            await screen_sizes(update, context, f["id"], args[1])
    elif action == "szr":
        storage.set_option(chat_id, int(args[0]), "sizes", None)
        await screen_size_groups(update, context, int(args[0]))
    elif action == "cd":
        await screen_conditions(update, context, int(args[0]))
    elif action == "cdt":
        f = storage.get_filter(chat_id, int(args[0]))
        if f:
            selected = conditions_of(f)
            sid = int(args[1])
            selected = [c for c in selected if c != sid] if sid in selected else selected + [sid]
            storage.set_option(chat_id, f["id"], "conditions", ",".join(map(str, selected)) or None)
            await screen_conditions(update, context, f["id"])
    elif action == "cdr":
        storage.set_option(chat_id, int(args[0]), "conditions", None)
        await screen_conditions(update, context, int(args[0]))
    elif action == "frg":
        await screen_filter_region(update, context, int(args[0]))
    elif action == "frs":
        f = storage.get_filter(chat_id, int(args[0]))
        domain = REGION_CODES.get(args[1], [VINTED_DOMAIN])[0]
        if f and f["domain"] != domain:
            storage.set_filter_domain(chat_id, f["id"], domain)
            await screen_filter(update, context, f["id"], note="✅ Регион изменён.\n\n")
        else:
            await screen_filter(update, context, int(args[0]))
    elif action == "ft":
        storage.toggle_filter(chat_id, int(args[0]))
        await screen_filter(update, context, int(args[0]))
    elif action == "fd":
        f = storage.get_filter(chat_id, int(args[0]))
        if f:
            await show(update, context, f"🗑 Удалить фильтр <b>{html.escape(f['brand_title'])}</b>?",
                       InlineKeyboardMarkup([[Btn("Да, удалить", callback_data=f"fdy|{f['id']}"),
                                              Btn("Отмена", callback_data=f"f|{f['id']}")]]))
    elif action == "fdy":
        storage.delete_filter(chat_id, int(args[0]))
        await screen_filters(update, context, note="✅ Фильтр удалён.\n\n")
    elif action == "fa":
        storage.set_all_enabled(chat_id, args[0] == "1")
        await screen_filters(update, context)
    elif action == "fxa":
        await show(update, context, "🧹 Удалить <b>все</b> фильтры? Это нельзя отменить.",
                   InlineKeyboardMarkup([[Btn("Да, удалить все", callback_data="fxay"),
                                          Btn("Отмена", callback_data="fl")]]))
    elif action == "fxay":
        storage.delete_all_filters(chat_id)
        await screen_filters(update, context, note="✅ Все фильтры удалены.\n\n")
    elif action == "pm":
        await screen_period_menu(update, context, args[0])
    elif action == "pv":
        await screen_period_item(update, context, int(args[0]))
    elif action == "it":
        await screen_items(update, context, args[0], int(args[1]))
    elif action in ("feed", "fav"):  # buttons on messages sent by older versions
        await screen_items(update, context, action, int(args[0]))
    elif action in ("rg", "rs"):  # the global region screen was replaced by a per-filter one
        await screen_main(update, context)
    elif action == "pause":
        storage.set_paused(chat_id, not storage.chat(chat_id)["paused"])
        await screen_main(update, context)
    elif action == "st":
        await screen_settings(update, context)
    elif action == "mf":
        storage.set_min_feedback(chat_id, int(args[0]))
        await screen_settings(update, context)
    elif action == "qt":
        storage.set_quiet(chat_id, not storage.chat(chat_id)["quiet"])
        await screen_settings(update, context)
    elif action == "bls":
        await screen_blocked(update, context)
    elif action == "ub":
        storage.unblock_seller(chat_id, int(args[0]))
        await screen_blocked(update, context)
    elif action == "clr":
        await show(update, context, "🧹 Очистить ленту найденных объявлений? Избранное останется.",
                   InlineKeyboardMarkup([[Btn("Да, очистить", callback_data="clry"),
                                          Btn("Отмена", callback_data="st")]]))
    elif action == "clry":
        storage.clear_found(chat_id)
        await screen_settings(update, context)


# --- monitoring ---

async def notify(context: ContextTypes.DEFAULT_TYPE, chat_id: int, item_id: int):
    r = storage.get_found(chat_id, item_id)
    if not r:
        return
    markup = notification_markup(chat_id, r)
    caption = "🆕 " + caption_for(r)
    try:
        if r["photo"]:
            await context.bot.send_photo(chat_id, r["photo"], caption=caption,
                                         parse_mode=ParseMode.HTML, reply_markup=markup)
            return
    except Exception as e:
        log.info("photo send failed, falling back to text: %s", e)
    await context.bot.send_message(chat_id, caption, parse_mode=ParseMode.HTML, reply_markup=markup)


async def check_filter(context: ContextTypes.DEFAULT_TYPE, f) -> int:
    client = vinted(f["domain"])
    items = await client.newest_items(f["brand_id"], f["price_from"], f["price_to"], **search_args(f))
    new_ids = set(storage.unseen(f["id"], [i.id for i in items]))
    top_id = max((i.id for i in items), default=0)
    record_clock(items)
    if not f["primed"]:
        # First run: remember what's already listed so the chat isn't flooded.
        storage.mark_seen(f["id"], list(new_ids))
        storage.mark_primed(f["id"], top_id)
        return 0
    # Bumped old listings jump to the top of "newest first" but keep their old (lower) id.
    if f["last_id"]:
        new_ids = {i for i in new_ids if i > f["last_id"]}
    storage.set_last_id(f["id"], top_id)
    chat_id = f["chat_id"]
    min_fb = MIN_FEEDBACK if f["min_feedback"] is None else f["min_feedback"]
    silent = bool(f["quiet"]) and is_quiet_now()
    sent = 0
    for item in sorted((i for i in items if i.id in new_ids), key=lambda i: i.id):
        if storage.is_blocked(chat_id, item.user_id):
            storage.mark_seen(f["id"], [item.id])
            continue
        try:
            seller = await client.seller(item.user_id)
            await asyncio.sleep(0.7)  # the profile API rate-limits bursts
        except VintedError:
            raise  # 429: stop this run; unseen items are retried next time
        except Exception as e:
            log.warning("seller %s lookup failed: %s", item.user_id, e)
            continue  # retry on the next run
        storage.mark_seen(f["id"], [item.id])
        if seller.feedback_count < min_fb:
            log.info("skip %s: seller %s has %d reviews", item.id, seller.login, seller.feedback_count)
            continue
        if not storage.add_found(chat_id, f["id"], item, seller):
            continue  # already found by another filter
        if not silent:
            await notify(context, chat_id, item.id)
        sent += 1
    return sent


async def run_checks(context: ContextTypes.DEFAULT_TYPE, chat_id: Optional[int] = None) -> int:
    total = 0
    async with lock("check"):
        started = time.time()
        active = storage.active_filters(chat_id)
        for f in active:
            try:
                total += await check_filter(context, f)
            except VintedError as e:
                log.warning("filter %s: %s", f["id"], e)
                if "429" in str(e):
                    break  # back off until the next run
            except Exception:
                log.exception("filter %s failed", f["id"])
            await asyncio.sleep(2)  # be gentle with Vinted
        state["last_check"] = datetime.datetime.now()
        log.info("checked %d filters in %.0fs, sent %d", len(active), time.time() - started, total)
    return total


async def check_all(context: ContextTypes.DEFAULT_TYPE):
    await run_checks(context)


async def on_startup(app: Application):
    # Seed the id clock from listings found so far (found shortly after being posted).
    if not storage.clock():
        for ts, item_id in storage.db.execute(
                "SELECT found_at, MAX(item_id) FROM found GROUP BY found_at / 60 ORDER BY found_at"):
            storage.add_clock(ts, item_id)
    await app.bot.set_my_commands([
        BotCommand("menu", "🏠 Главное меню"),
        BotCommand("new", "🔥 Новые объявления"),
        BotCommand("add", "➕ Добавить бренд"),
        BotCommand("list", "📋 Мои фильтры"),
        BotCommand("fav", "❤️ Избранное"),
        BotCommand("help", "❓ Как пользоваться"),
        BotCommand("app", "📱 Открыть приложение"),
    ])
    await webapp.start(app, sys.modules[__name__])


async def on_shutdown(app: Application):
    await webapp.stop(app)
    for client in clients.values():
        await client.close()


def main():
    # Persist chat/user data so a restart doesn't forget a half-finished "type the price" step.
    persistence = PicklePersistence(os.path.join(os.path.dirname(DB_PATH) or ".", "state.pickle"))
    app = (Application.builder().token(TOKEN).persistence(persistence)
           .post_init(on_startup).post_shutdown(on_shutdown).build())
    app.add_handler(TypeHandler(Update, log_update), group=-1)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("app", cmd_app))
    app.add_handler(CommandHandler("new", cmd_feed))
    app.add_handler(CommandHandler("fav", cmd_fav))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)
    app.job_queue.run_repeating(check_all, interval=CHECK_INTERVAL, first=10)
    log.info("bot started, checking every %ss", CHECK_INTERVAL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
