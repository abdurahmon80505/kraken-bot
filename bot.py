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
        "Baza guruhingizda topic yarating, nomiga manzil va interval yozing:\n"
        "<code>📱 Smartfonlar | https://t.me/Target/123 | 12</code>  (12 soat)\n"
        "<code>https://t.me/Target/456 | 2d</code>  (2 kun, nomsiz ham bo'ladi)\n\n"
        "So'ng o'sha topicga elon tashlang — bot interval bo'yicha avtomatik "
        "target guruhga forward qiladi.\n\n"
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
        hours = t["hours"]
        interval_str = f"{hours // 24} kun" if hours % 24 == 0 else f"{hours} soat"

        st = scheduler_state.get(t["id"])
        if st and st.get("last_time"):
            last = datetime.fromtimestamp(st["last_time"]).strftime("%d/%m %H:%M")
            nxt_ts = st["last_time"] + hours * 3600
            nxt = datetime.fromtimestamp(nxt_ts).strftime("%d/%m %H:%M")
        else:
            last = "hali yo'q"
            nxt  = "tez orada"

        name = t["name"]
        if len(name) > 30:
            name = name[:30] + "…"

        lines.append(
            f"📌 <b>{name}</b>\n"
            f"   📝 {len(msgs)} ta elon · ⏱ {interval_str}\n"
            f"   🕐 oxirgi: {last} · keyingi: {nxt}"
        )

    detail = "\n\n".join(lines) if lines else "<i>Aktiv topic yo'q</i>"

    await message.answer(
        f"📊 <b>Bot holati</b>\n\n"
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
                    continue  # Hali vaqti kelmadi

                messages = await get_topic_messages(tid)
                if not messages:
                    continue  # Bu topicda elon yo'q

                # Navbatdagi elonni tanlash (rotatsiya: 1, 2, 3, 1, 2, 3...)
                last_idx = state.get("last_sent_index", -1)
                next_idx = (last_idx + 1) % len(messages)
                msg      = messages[next_idx]

                ok, err = await request_forward(
                    message_id=msg["message_id"],
                    from_chat=SOURCE_GROUP,
                    topic_url=topic["url"],
                )

                if ok:
                    scheduler_state[tid] = {"last_sent_index": next_idx, "last_time": now}
                    log.info(f"✅ Topic#{tid} → {topic['url']} | elon#{next_idx+1}/{len(messages)}")
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            f"✅ Yuborildi: <b>{topic['name']}</b>\n"
                            f"📤 {topic['url']}\n"
                            f"📝 elon #{next_idx+1}/{len(messages)}\n"
                            f"🕐 {datetime.now().strftime('%d/%m %H:%M')}",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                else:
                    log.error(f"❌ Topic#{tid} forward xatosi: {err}")
                    # last_time ni yangilamaymiz — keyingi siklda qayta uriadi
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            f"❌ <b>{topic['name']}</b> yuborilmadi:\n<code>{err}</code>",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

        except Exception as e:
            log.error(f"Scheduler xatosi: {e}")

        await asyncio.sleep(60)  # Har daqiqada tekshir


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
