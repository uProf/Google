import asyncio
import io
import logging
import os
import re
import uuid

import httpx
from PIL import Image
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

# مقدارها از متغیر محیطی (environment) خونده میشن؛ برای اجرای محلی میتونی مستقیم اینجا بنویسی
BOT_TOKEN = os.getenv("BOT_TOKEN", "8982557183:AAEsGFPVq3Bg0ppL17phkuFnFH9y_TBZBsU")

# اختیاری ولی پیشنهاد میشه: پایدارترین منبع جستجو
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GOOGLE_CX = os.getenv("GOOGLE_CX", "")

MAX_RESULTS = 99      # تعداد نتایج هر جستجو
ALBUM_SIZE = 15        # تعداد عکس‌های آلبوم
MAX_SESSIONS = 5      # تعداد جستجوهای فعال برای هر گروه
DEBUG = False        # لاگ همه‌ی پیام‌هایی که به بات میرسه (بعد از رفع مشکل False کن)
WINDOW = 4            # چند عکس همزمان دانلود بشه
MAX_SIDE = 1280       # بزرگترین ضلع عکس بعد از کوچیک کردن

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

HTTP: httpx.AsyncClient | None = None


# ---------- جستجو ----------

async def google_search(query: str) -> list[str]:
    r = await HTTP.get(
        "https://www.googleapis.com/customsearch/v1",
        params={
            "key": GOOGLE_API_KEY,
            "cx": GOOGLE_CX,
            "q": query,
            "searchType": "image",
            "num": 10,
        },
    )
    r.raise_for_status()
    return [item["link"] for item in r.json().get("items", [])]


def _ddg_sync(query: str) -> list[str]:
    results = DDGS().images(query, max_results=MAX_RESULTS)
    return [r["image"] for r in results if r.get("image")]


async def ddg_search(query: str) -> list[str]:
    for attempt in range(2):
        try:
            urls = await asyncio.to_thread(_ddg_sync, query)
            if urls:
                return urls
        except Exception as e:
            log.warning("DDG attempt %d failed: %s", attempt + 1, e)
        await asyncio.sleep(1)
    return []


async def wiki_search(query: str) -> list[str]:
    """پشتیبان: عکس اصلی صفحه‌های ویکی‌پدیا (برای آدم‌ها و مکان‌های معروف)."""
    urls = []
    for lang in ("en", "fa"):
        try:
            r = await HTTP.get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "format": "json",
                    "generator": "search",
                    "gsrsearch": query,
                    "gsrlimit": 10,
                    "prop": "pageimages",
                    "piprop": "thumbnail",
                    "pithumbsize": 1280,
                },
            )
            pages = r.json().get("query", {}).get("pages", {}).values()
            for p in sorted(pages, key=lambda p: p.get("index", 99)):
                src = p.get("thumbnail", {}).get("source")
                if src:
                    urls.append(src)
        except Exception as e:
            log.warning("Wikipedia (%s) failed: %s", lang, e)
    return urls


async def search_images(query: str) -> list[str]:
    if GOOGLE_API_KEY and GOOGLE_CX:
        try:
            urls = await google_search(query)
            if urls:
                return list(dict.fromkeys(urls))
        except Exception as e:
            log.warning("Google search failed: %s", e)

    urls = await ddg_search(query)
    if not urls:
        urls = await wiki_search(query)
    return list(dict.fromkeys(urls))


# ---------- دانلود و آماده‌سازی عکس ----------

def prepare_image(raw: bytes) -> bytes | None:
    """عکس رو به JPEG سبک تبدیل میکنه تا آپلودش سریع باشه و تلگرام ردش نکنه."""
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        w, h = img.size
        if min(w, h) < 100 or max(w, h) / min(w, h) > 15:
            return None
        img = img.convert("RGB")
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=85)
        return out.getvalue()
    except Exception:
        return None


async def download_and_prepare(url: str) -> bytes | None:
    try:
        r = await asyncio.wait_for(HTTP.get(url), timeout=8)
        if r.status_code != 200 or len(r.content) > 15_000_000:
            return None
        return await asyncio.to_thread(prepare_image, r.content)
    except Exception:
        return None


def fetch_task(session: dict, j: int) -> asyncio.Task:
    """هر عکس فقط یک بار دانلود میشه و نتیجه‌اش نگه داشته میشه (پیش‌بارگذاری)."""
    task = session["tasks"].get(j)
    if task is None:
        task = asyncio.create_task(download_and_prepare(session["urls"][j]))
        session["tasks"][j] = task
    return task


async def find_valid(session: dict, start: int, step: int):
    """چند عکس رو همزمان دانلود میکنه و اولین عکس سالم (به ترتیب) رو برمیگردونه."""
    n = len(session["urls"])
    i = start
    while 0 <= i < n:
        idxs = [j for j in range(i, i + step * WINDOW, step) if 0 <= j < n]
        tasks = [fetch_task(session, j) for j in idxs]
        for j, t in zip(idxs, tasks):
            data = await t
            if data:
                return j, data
        i += step * WINDOW
    return None, None


# ---------- کمکی‌ها ----------

def keyboard(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ قبلی", callback_data=f"p:{sid}"),
            InlineKeyboardButton("➡️ بعدی", callback_data=f"n:{sid}"),
        ],
        [InlineKeyboardButton("📚 آلبوم", callback_data=f"a:{sid}")],
    ])


def caption(session: dict) -> str:
    return f"{session['q']}  ({session['i'] + 1}/{len(session['urls'])})"


# ---------- هندلرها ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "منو توی گروه اضافه کن و بنویس:\n/img لیدی گاگا"
    )


async def do_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    status = await update.message.reply_text("🔍 دارم میگردم...")
    try:
        await _search_and_send(update, context, query)
    finally:
        try:
            await status.delete()
        except TelegramError:
            pass


async def _search_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    msg = update.message
    urls = await search_images(query)
    if not urls:
        await msg.reply_text("چیزی پیدا نکردم 😕 یه جور دیگه بنویسش.")
        return

    session = {"q": query, "urls": urls, "i": 0, "tasks": {}}
    i, data = await find_valid(session, 0, 1)
    if data is None:
        await msg.reply_text("عکس قابل ارسالی پیدا نکردم 😕")
        return
    session["i"] = i

    sid = uuid.uuid4().hex[:8]
    sessions = context.chat_data.setdefault("sessions", {})
    sessions[sid] = session
    while len(sessions) > MAX_SESSIONS:
        old = sessions.pop(next(iter(sessions)))
        for t in old["tasks"].values():
            t.cancel()

    try:
        await msg.reply_photo(
            photo=data, caption=caption(session), reply_markup=keyboard(sid)
        )
    except TelegramError:
        log.exception("send photo failed")
        await msg.reply_text("ارسال عکس با خطا مواجه شد، دوباره امتحان کن.")


async def img_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    query = " ".join(context.args).strip()

    # اگه فقط /img زده بود و روی یه پیام ریپلای کرده بود، متن اون پیام رو سرچ کن
    if not query and msg.reply_to_message:
        query = (msg.reply_to_message.text or msg.reply_to_message.caption or "").strip()

    if not query:
        await msg.reply_text("مثال: /img لیدی گاگا")
        return

    await do_search(update, context, query)


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        action, sid = q.data.split(":")
    except ValueError:
        await q.answer()
        return

    session = context.chat_data.get("sessions", {}).get(sid)
    if not session:
        await q.answer("این جستجو منقضی شده، دوباره بفرست.", show_alert=True)
        return

    try:
        if action in ("n", "p"):
            step = 1 if action == "n" else -1
            i, data = await find_valid(session, session["i"] + step, step)
            if data is None:
                await q.answer("عکس دیگه‌ای نیست." if action == "n" else "این اولین عکسه.")
                return
            session["i"] = i
            await q.edit_message_media(
                media=InputMediaPhoto(data, caption=caption(session)),
                reply_markup=keyboard(sid),
            )
            await q.answer()

        elif action == "a":
            await q.answer("دارم آلبوم رو آماده میکنم...")
            s = session["i"]
            idxs = range(s, min(s + ALBUM_SIZE * 2, len(session["urls"])))
            results = await asyncio.gather(*(fetch_task(session, j) for j in idxs))
            photos = [d for d in results if d][:ALBUM_SIZE]

            if not photos:
                await q.message.reply_text("نتونستم عکسی برای آلبوم بگیرم.")
            elif len(photos) == 1:
                await q.message.reply_photo(photos[0])
            else:
                await q.message.reply_media_group([InputMediaPhoto(p) for p in photos])
    except TelegramError:
        log.exception("button action failed")


async def debug_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.effective_message
    chat = update.effective_chat
    log.info(
        "UPDATE chat=%s type=%s text=%r",
        chat.id if chat else None,
        chat.type if chat else None,
        m.text if m else None,
    )


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=context.error)


async def post_init(app: Application):
    global HTTP
    HTTP = httpx.AsyncClient(
        timeout=httpx.Timeout(8.0, connect=4.0),
        follow_redirects=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        },
        limits=httpx.Limits(max_connections=50),
    )


async def post_shutdown(app: Application):
    if HTTP:
        await HTTP.aclose()


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)   # چند نفر همزمان میتونن جستجو کنن
        .connect_timeout(15)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(15)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    if DEBUG:
        app.add_handler(MessageHandler(filters.ALL, debug_log), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("img", img_command, filters=filters.ChatType.GROUPS))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_error_handler(on_error)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
