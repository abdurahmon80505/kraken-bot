"""
Kraken Mobile — Bot
Vazifasi:
  1. SOURCE_GROUP dagi topic nomlarini o'qiydi
     Format: "📱 Nomi | https://t.me/Target/123 | 12"  (soat)
             "📱 Nomi | https://t.me/Target/123 | 2d"  (kun)
  2. O'sha topicga tashlangan elonlarni interval bo'yicha target ga yuboradi
  3. /vazifalar — barcha topiclarni ko'rsatadi (elon + Delete tugmasi)

Tuzatildi:
  * Qo'sh main() olib tashlandi — bitta toza main
  * Keep-alive: bot o'zini VA userbot ni har 10 daqiqada band tutadi
  * Userbot uxlab qolsa avtomatik uyg'otadi
  * Scheduler — userbot 502 bo'lsa kutadi, qayta uriadi
"""

import asyncio
import logging
import os
import re
import time
from datetime import datetime

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── ENV ────────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.environ["BOT_TOKEN"]
ADMIN_ID        = int(os.environ["ADMIN_ID"])
SOURCE_GROUP    = int(os.environ["SOURCE_GROUP"])
USERBOT_URL     = os.environ.get("USERBOT_URL", "").rstrip("/")
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")
PORT            = int(os.environ.get("PORT", 8081))
# O'zining tashqi URL i (keep-alive uchun). Render avtomatik beradi.
SELF_URL        = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()
router = Router()
dp.include_router(router)

# ── Topic nom parser ────────────────────────────────────────────────────────
# Linkdan oldingi "|" ixtiyoriy — "Nom | link | vaqt" ham, "link | vaqt" ham ishlaydi
TOPIC_RE = re.compile(r"(https://t\.me/[^\s|]+)\s*\|\s*(\d+)(d?)\s*$")


def parse_topic_name(name: str):
    m = TOPIC_RE.search(name or "")
    if not m:
        return None
    url    = m.group(1)
    num    = int(m.group(2))
    is_day = m.group(3) == "d"
    hours  = num * 24 if is_day else num
    return {"url": url, "hours": hours}


# ── Userbot ga forward so'rovi ──────────────────────────────────────────────
async def request_forward(message_id: int, from_chat: int, topic_url: str) -> bool:
    if not USERBOT_URL:
        log.error("USERBOT_URL ENV o'rnatilmagan!")
        return False

    payload = {
        "message_id": message_id,
        "from_chat":  str(from_chat),
        "topic_url":  topic_url,
        "secret":     INTERNAL_SECRET,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{USERBOT_URL}/forward",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=40),
            ) as resp:
                result = await resp.json()
        if result.get("ok"):
            return True
        log.error(f"Forward xatosi: {result.get('error')}")
        return False
    except Exception as e:
        log.error(f"Userbot bilan bog'lanib bo'lmadi: {e}")
        return False


# ── Guruhdan topiclarni olish (502 bo'lsa kutadi) ───────────────────────────
async def get_topics() -> list:
    if not USERBOT_URL:
        return []
    for attempt in range(4):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{USERBOT_URL}/topics",
                    params={"secret": INTERNAL_SECRET, "group_id": SOURCE_GROUP},
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status in (502, 503):
                        log.warning(f"Userbot uyg'onmoqda... ({attempt+1}/4)")
                        await asyncio.sleep(15)
                        continue
                    data = await resp.json()
            topics = []
            for t in data.get("topics", []):
                parsed = parse_topic_name(t.get("title", ""))
                if parsed:
                    topics.append({
                        "id":    t["id"],
                        "name":  t["title"],
                        "url":   parsed["url"],
                        "hours": parsed["hours"],
                    })
            return topics
        except Exception as e:
            log.error(f"Topiclar yuklanmadi ({attempt+1}/4): {e}")
            await asyncio.sleep(10)
    return []


# ── Topicdan elonlarni olish ────────────────────────────────────────────────
async def get_topic_messages(topic_id: int) -> list:
    if not USERBOT_URL:
        return []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{USERBOT_URL}/messages",
                params={
                    "secret":   INTERNAL_SECRET,
                    "group_id": SOURCE_GROUP,
                    "topic_id": topic_id,
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
        return data.get("messages", [])
    except Exception as e:
        log.error(f"Xabarlar yuklanmadi: {e}")
        return []


# ── /start ──────────────────────────────────────────────────────────────────
@router.message(Command("start"))
async def cmd_start(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "👋 <b>Kraken Bot</b>\n\n"
        "Guruhingizda topiclar yarating:\n"
        "<code>📱 Smartfonlar | https://t.me/Target/123 | 12</code>\n"
        "<code>💻 Noutbuklar | https://t.me/Target/456 | 2d</code>\n\n"
        "<b>Komandalar:</b>\n"
        "/vazifalar — barcha aktiv topiclar\n"
        "/status — bot holati",
        parse_mode="HTML"
    )


# ── /status ─────────────────────────────────────────────────────────────────
@router.message(Command("status"))
async def cmd_status(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    userbot_status = "❓"
    if USERBOT_URL:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{USERBOT_URL}/health",
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    userbot_status = "✅ Ishlayapti" if resp.status == 200 else "❌ Xato"
        except Exception:
            userbot_status = "❌ Ulanmayapti"
    else:
        userbot_status = "❌ USERBOT_URL yo'q"

    topics = await get_topics()

    # Scheduler holati
    lines = []
    for t in topics:
        st = scheduler_state.get(t["id"])
        if st and st.get("last_time"):
            last = datetime.fromtimestamp(st["last_time"]).strftime("%d/%m %H:%M")
        else:
            last = "hali yo'q"
        lines.append(f"• {t['name'][:25]} — oxirgi: {last}")

    detail = "\n".join(lines) if lines else "—"

    await message.answer(
        f"📊 <b>Bot holati</b>\n\n"
        f"🤖 Userbot: {userbot_status}\n"
        f"📋 Aktiv topiclar: {len(topics)} ta\n\n"
        f"{detail}",
        parse_mode="HTML"
    )


# ── /vazifalar ──────────────────────────────────────────────────────────────
@router.message(Command("vazifalar"))
async def cmd_vazifalar(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    await message.answer("⏳ Topiclar yuklanmoqda...")

    topics = await get_topics()
    if not topics:
        await message.answer(
            "📭 Aktiv topic topilmadi.\n\n"
            "Topic nomini shunday yozing:\n"
            "<code>📱 Smartfonlar | https://t.me/Target/123 | 12</code>",
            parse_mode="HTML"
        )
        return

    for topic in topics:
        messages = await get_topic_messages(topic["id"])

        hours = topic["hours"]
        interval_str = f"{hours // 24} kun" if hours % 24 == 0 else f"{hours} soat"

        header = (
            f"📌 <b>{topic['name']}</b>\n"
            f"📤 {topic['url']}\n"
            f"⏱ Interval: {interval_str}\n"
            f"📝 Elonlar: {len(messages)} ta\n"
        )

        if not messages:
            await message.answer(
                header + "\n<i>Elon yo'q</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🗑 Topicni o'chirish",
                        callback_data=f"del_topic:{topic['id']}"
                    )
                ]])
            )
            continue

        await message.answer(header, parse_mode="HTML")

        for i, msg in enumerate(messages):
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="🗑 O'chirish",
                    callback_data=f"del_msg:{SOURCE_GROUP}:{msg['message_id']}:{topic['id']}"
                )
            ]])

            try:
                await bot.forward_message(
                    chat_id=ADMIN_ID,
                    from_chat_id=SOURCE_GROUP,
                    message_id=msg["message_id"],
                )
                await bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"☝️ Elon #{i+1}",
                    reply_markup=keyboard,
                )
            except Exception as e:
                await message.answer(f"❌ Elon #{i+1} yuklanmadi: {e}", reply_markup=keyboard)

            await asyncio.sleep(0.5)


# ── O'chirish tugmasi (elon) ─────────────────────────────────────────────────
@router.callback_query(F.data.startswith("del_msg:"))
async def on_delete_message(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Ruxsat yo'q")
        return

    parts      = callback.data.split(":")
    group_id   = int(parts[1])
    message_id = int(parts[2])
    topic_id   = int(parts[3])

    try:
        await bot.delete_message(chat_id=group_id, message_id=message_id)
        await callback.message.delete()
        await callback.answer("✅ Elon o'chirildi")
        log.info(f"Elon o'chirildi: msg#{message_id} topic#{topic_id}")
    except Exception as e:
        await callback.answer(f"❌ O'chirib bo'lmadi: {e}", show_alert=True)


# ── O'chirish tugmasi (topic) ────────────────────────────────────────────────
@router.callback_query(F.data.startswith("del_topic:"))
async def on_delete_topic(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Ruxsat yo'q")
        return

    await callback.message.delete()
    await callback.answer("ℹ️ Topicni guruhdan o'chiring yoki nomini o'zgartiring")


# ── SCHEDULER ─────────────────────────────────────────────────────────────────
scheduler_state: dict = {}


async def wake_userbot():
    """Userbot uxlab qolgan bo'lsa uyg'otadi."""
    if not USERBOT_URL:
        return False
    for attempt in range(12):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{USERBOT_URL}/health",
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        log.info("Userbot uyg'oq ✓")
                        return True
        except Exception:
            pass
        log.info(f"Userbot uyg'onishini kutmoqda... ({attempt+1}/12)")
        await asyncio.sleep(10)
    log.error("Userbot uyg'onmadi!")
    return False


async def scheduler_loop():
    log.info("Scheduler ishga tushdi")
    await asyncio.sleep(10)
    await wake_userbot()

    while True:
        try:
            topics = await get_topics()

            for topic in topics:
                tid      = topic["id"]
                interval = topic["hours"] * 3600

                state     = scheduler_state.get(tid, {"last_sent_index": -1, "last_time": 0})
                now       = time.time()
                last_time = state.get("last_time", 0)

                if now - last_time < interval:
                    continue

                messages = await get_topic_messages(tid)
                if not messages:
                    continue

                last_idx = state.get("last_sent_index", -1)
                next_idx = (last_idx + 1) % len(messages)
                msg      = messages[next_idx]

                success = await request_forward(
                    message_id=msg["message_id"],
                    from_chat=SOURCE_GROUP,
                    topic_url=topic["url"],
                )

                if success:
                    scheduler_state[tid] = {"last_sent_index": next_idx, "last_time": now}
                    log.info(f"✅ Topic#{tid} → {topic['url']} | elon#{next_idx+1}/{len(messages)}")
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            f"✅ Yuborildi: <b>{topic['name']}</b>\n"
                            f"📤 {topic['url']}\n"
                            f"🕐 {datetime.now().strftime('%H:%M')}",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                else:
                    log.error(f"❌ Topic#{tid} forward xatosi")

        except Exception as e:
            log.error(f"Scheduler xatosi: {e}")

        await asyncio.sleep(60)


# ── KEEP-ALIVE — bot + userbot uxlamasligi uchun ──────────────────────────────
async def keepalive_loop():
    """
    Har 10 daqiqada:
      1. Userbot /health ni ping — userbot uxlamaydi.
      2. O'zining SELF_URL ini ping — bot ham uxlamaydi
         (Render web service 15 daqiqada uxlaydi, shuning oldini olamiz).
    """
    await asyncio.sleep(30)
    while True:
        # Userbot ni band tut
        if USERBOT_URL:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{USERBOT_URL}/health",
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        log.info(f"Keep-alive userbot: {resp.status}")
            except Exception as e:
                log.warning(f"Keep-alive userbot xato: {e}")

        # O'zini band tut
        if SELF_URL:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{SELF_URL}/health",
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as resp:
                        log.info(f"Keep-alive self: {resp.status}")
            except Exception as e:
                log.warning(f"Keep-alive self xato: {e}")

        await asyncio.sleep(600)  # 10 daqiqa


# ── Health server (Render uchun + keep-alive) ────────────────────────────────
async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "bot": "kraken-bot"})


async def start_health_server():
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Health server: port {PORT}")


# ── Main (bitta, toza, auto-restart bilan) ───────────────────────────────────
async def main():
    log.info("Bot ishga tushmoqda (auto-restart rejimi)...")
    await start_health_server()
    asyncio.create_task(scheduler_loop())
    asyncio.create_task(keepalive_loop())

    while True:
        try:
            log.info("Polling boshlandi...")
            await dp.start_polling(bot, handle_signals=False)
        except Exception as e:
            log.error(f"Polling xatosi: {e} — 5 soniyadan keyin qayta urinadi")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
