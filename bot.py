"""
Kraken Mobile — Bot (SODDALASHTIRILGAN)
Vazifasi:
  * SOURCE_GROUP (baza guruh) dagi topic nomlarini o'qiydi
    Format: "Nomi | https://t.me/Target/123 | 12"  (soat)
            "https://t.me/Target/123 | 2d"         (kun, nomsiz ham bo'ladi)
  * Har topic dagi elonlarni interval bo'yicha AVTOMATIK target ga forward qiladi
    (forward'ni userbot bajaradi — premium emoji saqlanadi)

  /start  — yordam
  /status — bot va userbot holati, oxirgi yuborilgan vaqtlar

ESLATMA: /vazifalar olib tashlandi — baza guruhni o'zingiz ko'rasiz.
         Bot endi guruhdan xabar forward qilmaydi, faqat userbotga buyruq beradi.
         Shu sababli "message to forward not found" xatosi endi yo'q.
"""

import asyncio
import logging
import os
import re
import time
from datetime import datetime

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message

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
SELF_URL        = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()
router = Router()
dp.include_router(router)

# ── Topic nom parser ────────────────────────────────────────────────────────
# Format:  Nom | https://t.me/Target/123 | 1d | 16:00
#   * "1d"  = har 1 kunda (3d = har 3 kunda). Faqat "1" yozilsa ham 1 kun.
#   * "16:00" = Tashkent vaqti (UTC+5) bilan har kungi yuborish soati.
# Vaqt yozilmasa — default 09:00. Eski "| 1d" format ham ishlaydi.
TOPIC_RE = re.compile(
    r"(https://t\.me/[^\s|]+)\s*\|\s*(\d+)\s*d?\s*(?:\|\s*(\d{1,2}):(\d{2}))?\s*$"
)


def parse_topic_name(name: str):
    m = TOPIC_RE.search(name or "")
    if not m:
        return None
    url  = m.group(1)
    days = int(m.group(2))
    if days < 1:
        days = 1
    if m.group(3) is not None:
        hh = int(m.group(3))
        mm = int(m.group(4))
        if hh > 23 or mm > 59:
            hh, mm = 9, 0
    else:
        hh, mm = 9, 0
    return {"url": url, "days": days, "hh": hh, "mm": mm}


# ── Userbot ga forward so'rovi ──────────────────────────────────────────────
async def request_forward(message_id: int, from_chat: int, topic_url: str):
    """
    Userbot ga POST /forward yuboradi.
    Qaytaradi: (True, None) yoki (False, "xato matni")
    """
    if not USERBOT_URL:
        return False, "USERBOT_URL yo'q"

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
            return True, None
        return False, result.get("error", "noma'lum xato")
    except Exception as e:
        return False, str(e)


# ── Guruhdan topiclarni olish (502 bo'lsa kutadi) ───────────────────────────
async def get_topics():
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
                        "id":   t["id"],
                        "name": t["title"],
                        "url":  parsed["url"],
                        "days": parsed["days"],
                        "hh":   parsed["hh"],
                        "mm":   parsed["mm"],
                    })
            return topics
        except Exception as e:
            log.error(f"Topiclar yuklanmadi ({attempt+1}/4): {e}")
            await asyncio.sleep(10)
    return []


# ── Topicdan elonlarni olish ────────────────────────────────────────────────
async def get_topic_messages(topic_id: int):
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
        "Baza guruhda topic yarating. Topic nomiga: manzil | kun | soat\n\n"
        "<code>📱 Smartfonlar | https://t.me/Target/123 | 1d | 16:00</code>\n"
        "<code>💻 Noutbuklar | https://t.me/Target/456 | 3d | 09:30</code>\n\n"
        "Ya'ni:\n"
        "• <b>1d</b> = har kuni, <b>3d</b> = har 3 kunda\n"
        "• <b>16:00</b> = Tashkent vaqti bilan yuborish soati\n\n"
        "Bot har kuni o'sha soatda topicdagi <b>hamma elonni</b> target "
        "guruhga forward qiladi.\n\n"
        "/status — holatni ko'rish",
        parse_mode="HTML"
    )


# ── /status ─────────────────────────────────────────────────────────────────
@router.message(Command("status"))
async def cmd_status(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    # Userbot holati
    userbot_status = "❌ Ulanmayapti"
    if not USERBOT_URL:
        userbot_status = "❌ USERBOT_URL yo'q"
    else:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{USERBOT_URL}/health",
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 200:
                        userbot_status = "✅ Ishlayapti"
        except Exception:
            pass

    topics = await get_topics()

    lines = []
    for t in topics:
        msgs = await get_topic_messages(t["id"])
        days = t["days"]
        interval_str = "har kuni" if days == 1 else f"har {days} kun"
        time_str = f"{t['hh']:02d}:{t['mm']:02d}"

        st = scheduler_state.get(t["id"])
        if st and st.get("last_time"):
            last = datetime.utcfromtimestamp(st["last_time"] + 5*3600).strftime("%d/%m %H:%M")
        else:
            last = "hali yo'q"

        name = t["name"]
        if len(name) > 30:
            name = name[:30] + "…"

        lines.append(
            f"📌 <b>{name}</b>\n"
            f"   📝 {len(msgs)} ta elon · ⏱ {interval_str} soat {time_str}\n"
            f"   🕐 oxirgi: {last}"
        )

    detail = "\n\n".join(lines) if lines else "<i>Aktiv topic yo'q</i>"

    await message.answer(
        f"📊 <b>Bot holati</b> (Tashkent vaqti)\n\n"
        f"🤖 Userbot: {userbot_status}\n"
        f"📋 Aktiv topiclar: {len(topics)} ta\n\n"
        f"{detail}",
        parse_mode="HTML"
    )


# ── SCHEDULER — avtomatik yuborish (asosiy ish) ──────────────────────────────
scheduler_state: dict = {}


async def wake_userbot():
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

    # Tashkent vaqti = UTC + 5 soat
    TASHKENT_OFFSET = 5 * 3600

    while True:
        try:
            topics = await get_topics()
            now = time.time()
            # Hozirgi Tashkent vaqti
            tk_now = datetime.utcfromtimestamp(now + TASHKENT_OFFSET)

            for topic in topics:
                tid  = topic["id"]
                days = topic["days"]
                hh   = topic["hh"]
                mm   = topic["mm"]

                state     = scheduler_state.get(tid, {"last_time": 0})
                last_time = state.get("last_time", 0)

                # 1) Hozir belgilangan SOAT-DAQIQA keldimi? (Tashkent bo'yicha)
                #    1 daqiqalik oyna: hh:mm ga teng bo'lsa.
                if not (tk_now.hour == hh and tk_now.minute == mm):
                    continue

                # 2) Oxirgi yuborishdan kamida (days) kun o'tdimi?
                #    days*24 soatdan biroz kam (23.5 soat) — bir kunda 2 marta
                #    yubormaslik uchun, lekin keyingi kun o'sha soatda o'tkazib
                #    yubormaslik uchun.
                min_gap = days * 24 * 3600 - 1800  # yarim soat zahira
                if now - last_time < min_gap:
                    continue  # Bu davrda allaqachon yuborilgan

                # 3) Bu topicdagi HAMMA elonni olamiz
                messages = await get_topic_messages(tid)
                if not messages:
                    continue

                log.info(f"⏰ Topic#{tid} vaqti keldi ({hh:02d}:{mm:02d}) — {len(messages)} ta elon yuborilmoqda")

                sent = 0
                failed = 0
                last_err = None
                for idx, msg in enumerate(messages):
                    ok, err = await request_forward(
                        message_id=msg["message_id"],
                        from_chat=SOURCE_GROUP,
                        topic_url=topic["url"],
                    )
                    if ok:
                        sent += 1
                        log.info(f"  ✅ {idx+1}/{len(messages)} → {topic['url']}")
                    else:
                        failed += 1
                        last_err = err
                        log.error(f"  ❌ {idx+1}/{len(messages)}: {err}")
                    # Telegram flood limitiga urilmaslik uchun har elon orasida pauza
                    await asyncio.sleep(3)

                # Yuborildi deb belgilaymiz (hatto qisman bo'lsa ham — qayta
                # yubormaslik uchun; xatolarni adminga aytamiz)
                scheduler_state[tid] = {"last_time": now}

                # Adminga hisobot
                try:
                    report = (
                        f"📤 <b>{topic['name']}</b>\n"
                        f"🎯 {topic['url']}\n"
                        f"✅ Yuborildi: {sent} ta"
                    )
                    if failed:
                        report += f"\n❌ Xato: {failed} ta\n<code>{last_err}</code>"
                    report += f"\n🕐 {tk_now.strftime('%d/%m %H:%M')} (Tashkent)"
                    await bot.send_message(ADMIN_ID, report, parse_mode="HTML")
                except Exception:
                    pass

        except Exception as e:
            log.error(f"Scheduler xatosi: {e}")

        # Har 30 soniyada tekshir (daqiqa oynasini o'tkazib yubormaslik uchun)
        await asyncio.sleep(30)


# ── KEEP-ALIVE — bot + userbot uxlamasligi uchun ──────────────────────────────
async def keepalive_loop():
    await asyncio.sleep(30)
    while True:
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


# ── Health server (Render uchun) ─────────────────────────────────────────────
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


# ── Main ─────────────────────────────────────────────────────────────────────
async def main():
    log.info("Bot ishga tushmoqda...")
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
