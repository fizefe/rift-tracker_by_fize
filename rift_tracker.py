"""
Asura Rift Tracker — Roblox Game Server Monitor
Запуск: python rift_tracker.py
"""

import asyncio
import aiohttp
import logging
import json
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

# ─────────────────────────────────────────
#  НАСТРОЙКИ
# ─────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "ВАШ_ТОКЕН_БОТА")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "ВАШ_CHAT_ID")

PLACE_ID           = 13358463560
CHECK_INTERVAL_SEC = 5 * 60

RIFT_CYCLE_SEC     = 5400
RIFT_ACTIVE_SEC    = 5400
RIFT_WINDOW_START  = 5400
SERVER_MAX_AGE_H   = 48

STATE_FILE = Path("server_state.json")
# ─────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("rift_tracker")

# Заголовки которые принимает Roblox
ROBLOX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.roblox.com/",
    "Origin": "https://www.roblox.com",
}

ROBLOX_SERVERS_URL = (
    "https://games.roblox.com/v1/games/{place_id}/servers/Public"
    "?sortOrder=Asc&limit=100&cursor={cursor}"
)


# ──────────────────────────────────────────────────────────────
#  ХРАНИЛИЩЕ СЕРВЕРОВ
# ──────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            log.info("Загружено %d серверов из state-файла", len(data))
            return data
        except Exception as e:
            log.warning("Не удалось загрузить state-файл: %s", e)
    return {}


def save_state(state: dict) -> None:
    try:
        with STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("Не удалось сохранить state-файл: %s", e)


def now_utc_str() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_utc(iso_str: str) -> datetime:
    return datetime.fromisoformat(iso_str)


# ──────────────────────────────────────────────────────────────
#  ROBLOX API
# ──────────────────────────────────────────────────────────────

async def fetch_servers(session: aiohttp.ClientSession, place_id: int) -> list[dict]:
    servers = []
    cursor = ""

    while True:
        url = ROBLOX_SERVERS_URL.format(place_id=place_id, cursor=cursor)
        try:
            async with session.get(
                url,
                headers=ROBLOX_HEADERS,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 400:
                    log.warning("Roblox вернул 400 — возможно нужна авторизация")
                    break
                if resp.status != 200:
                    log.warning("Roblox API статус %s", resp.status)
                    break
                data = await resp.json()
        except Exception as e:
            log.error("Ошибка запроса к Roblox API: %s", e)
            break

        batch = data.get("data", [])
        servers.extend(batch)

        cursor = data.get("nextPageCursor") or ""
        if not cursor:
            break
        await asyncio.sleep(0.5)

    return servers


async def fetch_servers_with_cookie(
    session: aiohttp.ClientSession,
    place_id: int,
    roblosecurity: str,
) -> list[dict]:
    """Запрос с .ROBLOSECURITY куки — обходит блокировку Roblox."""
    servers = []
    cursor = ""
    cookie_jar = aiohttp.CookieJar()
    cookie_jar.update_cookies(
        {".ROBLOSECURITY": roblosecurity},
        response_url=aiohttp.client.URL("https://roblox.com"),
    )

    while True:
        url = ROBLOX_SERVERS_URL.format(place_id=place_id, cursor=cursor)
        try:
            async with session.get(
                url,
                headers=ROBLOX_HEADERS,
                timeout=aiohttp.ClientTimeout(total=20),
                cookies={".ROBLOSECURITY": roblosecurity},
            ) as resp:
                if resp.status != 200:
                    log.warning("Roblox API статус %s (с куки)", resp.status)
                    break
                data = await resp.json()
        except Exception as e:
            log.error("Ошибка запроса (с куки): %s", e)
            break

        batch = data.get("data", [])
        servers.extend(batch)

        cursor = data.get("nextPageCursor") or ""
        if not cursor:
            break
        await asyncio.sleep(0.5)

    return servers


# ──────────────────────────────────────────────────────────────
#  ЛОГИКА РИФТОВ
# ──────────────────────────────────────────────────────────────

def server_age_seconds(server_id: str, state: dict) -> Optional[float]:
    entry = state.get(server_id)
    if not entry:
        return None
    try:
        first_seen = parse_utc(entry["first_seen"])
        return (datetime.now(timezone.utc) - first_seen).total_seconds()
    except Exception as e:
        log.debug("Ошибка возраста сервера %s: %s", server_id, e)
        return None


def rift_status(age_sec: float) -> dict:
    cycle = RIFT_CYCLE_SEC * 2

    if age_sec < RIFT_WINDOW_START:
        return {
            "active": False,
            "seconds_left": 0,
            "next_rift_in": RIFT_WINDOW_START - age_sec,
            "rift_number": 1,
        }

    t = age_sec - RIFT_WINDOW_START
    phase = t % cycle

    if phase < RIFT_ACTIVE_SEC:
        return {
            "active": True,
            "seconds_left": RIFT_ACTIVE_SEC - phase,
            "next_rift_in": 0,
            "rift_number": int(t // cycle) + 1,
        }

    return {
        "active": False,
        "seconds_left": 0,
        "next_rift_in": cycle - phase,
        "rift_number": int(t // cycle) + 2,
    }


# ──────────────────────────────────────────────────────────────
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ──────────────────────────────────────────────────────────────

def format_time(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ч {m:02d}м {s:02d}с"
    elif m:
        return f"{m}м {s:02d}с"
    return f"{s}с"


def build_join_link(server_id: str) -> str:
    # Deeplink который открывает Roblox сразу на нужном сервере
    return f"roblox://experiences/start?placeId={PLACE_ID}&gameInstanceId={server_id}"


# ──────────────────────────────────────────────────────────────
#  TELEGRAM
# ──────────────────────────────────────────────────────────────

async def send_telegram(session: aiohttp.ClientSession, text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        async with session.post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            result = await r.json()
            if not result.get("ok"):
                log.error("Telegram ошибка: %s", result)
                return False
            return True
    except Exception as e:
        log.error("Ошибка отправки в Telegram: %s", e)
        return False


async def notify_rift_server(
    session: aiohttp.ClientSession,
    server: dict,
    age_sec: float,
    status: dict,
) -> None:
    srv_id = server.get("id", "?")
    join = build_join_link(srv_id)
    left = format_time(status["seconds_left"])

    players_raw = server.get("playing", server.get("playerTokens", []))
    if isinstance(players_raw, list):
        player_count = len(players_raw)
    elif isinstance(players_raw, (int, float)):
        player_count = int(players_raw)
    else:
        player_count = 0

    deep_link = f"roblox://experiences/start?placeId={PLACE_ID}&gameInstanceId={srv_id}"
    web_link = f"https://www.roblox.com/games/{PLACE_ID}?jobId={srv_id}"

    text = (
        f"🌀 <b>РИФТ АКТИВЕН!</b> (#{status['rift_number']})\n\n"
        f"⏳ <b>Сервер существует:</b> {format_time(age_sec)} "
        f"({age_sec / 3600:.2f}ч)\n"
        f"👥 <b>Игроков:</b> {player_count}/{server.get('maxPlayers', '?')}\n\n"
        f"🎮 <b>Войти на сервер:</b>\n"
        f'<a href="{deep_link}">▶ Открыть в Roblox (ПК/телефон)</a>\n\n'
        f"📋 <b>Или скопируй ссылку:</b>\n"
        f"<code>{web_link}</code>"
    )

    ok = await send_telegram(session, text)
    if ok:
        log.info("✅ Отправлено | сервер %s... | рифт #%d | осталось %s",
                 srv_id[:12], status["rift_number"], left)


# ──────────────────────────────────────────────────────────────
#  ОСНОВНОЙ ЦИКЛ
# ──────────────────────────────────────────────────────────────

async def check_once(
    session: aiohttp.ClientSession,
    state: dict,
    roblosecurity: str,
) -> None:
    log.info("═══ Проверка серверов ═══")

    if roblosecurity:
        servers = await fetch_servers_with_cookie(session, PLACE_ID, roblosecurity)
    else:
        servers = await fetch_servers(session, PLACE_ID)

    if not servers:
        log.warning("Серверы не получены")
        return

    now_str = now_utc_str()
    active_ids: set[str] = set()
    rift_found = 0
    new_servers = 0

    for srv in servers:
        srv_id = srv.get("id")
        if not srv_id:
            continue

        active_ids.add(srv_id)

        if srv_id not in state:
            state[srv_id] = {"first_seen": now_str, "notified": []}
            new_servers += 1

        age = server_age_seconds(srv_id, state)
        if age is None or age > SERVER_MAX_AGE_H * 3600:
            continue

        status = rift_status(age)
        already_notified: list = state[srv_id].get("notified", [])

        if status["active"] and status["rift_number"] not in already_notified:
            await notify_rift_server(session, srv, age, status)
            already_notified.append(status["rift_number"])
            state[srv_id]["notified"] = already_notified
            rift_found += 1
            await asyncio.sleep(0.5)

    gone = set(state.keys()) - active_ids
    for srv_id in gone:
        del state[srv_id]

    save_state(state)
    log.info("Серверов=%d | новых=%d | рифтов=%d | в state=%d",
             len(servers), new_servers, rift_found, len(state))


async def main() -> None:
    log.info("🚀 Rift Tracker | PlaceID=%d | интервал=%s",
             PLACE_ID, format_time(CHECK_INTERVAL_SEC))

    if "ВАШ" in TELEGRAM_BOT_TOKEN or "ВАШ" in TELEGRAM_CHAT_ID:
        log.error("❌ Не заданы TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID!")
        return

    # .ROBLOSECURITY кука — задай здесь или через переменную окружения
    # Как получить: F12 в браузере на roblox.com → Application → Cookies
    roblosecurity = os.environ.get("ROBLOSECURITY", "")

    if not roblosecurity:
        log.warning("⚠️  ROBLOSECURITY не задана — API может вернуть 400")
        log.warning("   Получи куку из браузера и задай переменную ROBLOSECURITY")

    state = load_state()

    async with aiohttp.ClientSession() as session:
        await check_once(session, state, roblosecurity)

        while True:
            log.info("💤 Следующая проверка через %s", format_time(CHECK_INTERVAL_SEC))
            await asyncio.sleep(CHECK_INTERVAL_SEC)
            await check_once(session, state, roblosecurity)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлено.")
