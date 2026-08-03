"""
Telegram-бот для курьеров (вебхук-версия): отдаёт адрес точки Б текущего
активного заказа (Yandex Fleet API) по кнопке.

Работает как постоянный веб-сервис (Flask + systemd), а не по расписанию —
Telegram сам стучится на /webhook при каждом сообщении, ответ мгновенный.
Живёт на RU VDS (176.113.82.49), а не на десктопе — независимо от того,
включён ли компьютер Андрея.

Все ключи — из переменных окружения (systemd EnvironmentFile), в коде их нет.
Идентификация курьера — ТОЛЬКО через Telegram contact-share (см. пояснение
в исходной локальной версии tools/courier_pointb_bot.py в репозитории
my_claude_life) — номер нельзя подставить чужим.
"""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from flask import Flask, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("courier_bot")

FLEET_BASE_URL = "https://fleet-api.taxi.yandex.net"
TELEGRAM_BASE_URL = "https://api.telegram.org"

STATE_DIR = Path(__file__).resolve().parent / "state"
STATE_DIR.mkdir(exist_ok=True)
VERIFIED_PATH = STATE_DIR / "verified.json"
PHONE_INDEX_PATH = STATE_DIR / "phone_index.json"

PARKS = {
    "dostavator": {
        "client_id": os.environ["YANDEX_CLIENT_ID"],
        "api_key": os.environ["YANDEX_API_KEY"],
        "park_id": os.environ["YANDEX_PARK_ID"],
    },
    "dostavatorplus": {
        "client_id": os.environ["YANDEX_CLIENT_ID_PLUS"],
        "api_key": os.environ["YANDEX_API_KEY_PLUS"],
        "park_id": os.environ["YANDEX_PARK_ID_PLUS"],
    },
}
BOT_TOKEN = os.environ["COURIER_BOT_TOKEN"]

ACTIVE_STATUS_PRIORITY = ["transporting", "driving", "waiting"]
ORDERS_LOOKBACK_HOURS = 6
PHONE_INDEX_REFRESH_SECONDS = 6 * 3600

POINT_B_BUTTON_TEXT = "📍 Где точка Б"
POINT_B_KEYBOARD = {"keyboard": [[{"text": POINT_B_BUTTON_TEXT}]], "resize_keyboard": True}
SHARE_CONTACT_KEYBOARD = {
    "keyboard": [[{"text": "Поделиться номером", "request_contact": True}]],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}

_lock = threading.Lock()  # верифицированные/индекс правятся из одного потока за раз
_verified_cache = None
_phone_index_cache = None
_phone_index_built_at = 0.0


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_verified() -> dict:
    global _verified_cache
    if _verified_cache is None:
        _verified_cache = load_json(VERIFIED_PATH, {})
    return _verified_cache


def save_verified(store: dict) -> None:
    save_json(VERIFIED_PATH, store)


def fleet_headers(creds: dict) -> dict:
    return {
        "X-Client-ID": creds["client_id"],
        "X-API-Key": creds["api_key"],
        "Content-Type": "application/json",
        "Accept-Language": "ru",
    }


def fleet_post(url: str, creds: dict, body: dict, max_retries: int = 6) -> dict:
    delay = 2.0
    for _ in range(max_retries):
        resp = requests.post(url, headers=fleet_headers(creds), json=body, timeout=30)
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = float(resp.headers.get("Retry-After", delay))
            log.info(f"  {resp.status_code}, жду {wait:.0f}с...")
            time.sleep(wait)
            delay = min(delay * 2, 30)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    return None


def fetch_driver_phone_index() -> dict:
    index = {}
    for park_key, creds in PARKS.items():
        url = f"{FLEET_BASE_URL}/v1/parks/driver-profiles/list"
        offset = 0
        limit = 500
        while True:
            body = {"query": {"park": {"id": creds["park_id"]}}, "limit": limit, "offset": offset}
            data = fleet_post(url, creds, body)
            items = data.get("driver_profiles", [])
            for item in items:
                info = item.get("driver_profile", {})
                driver_id = info.get("id")
                name = " ".join(
                    filter(None, [info.get("last_name"), info.get("first_name"), info.get("middle_name")])
                )
                for phone in info.get("phones", []):
                    index[phone] = {"park": park_key, "id": driver_id, "name": name}
            total = data.get("total", 0)
            offset += len(items)
            if not items or offset >= total:
                break
            time.sleep(1.0)
        log.info(f"  {park_key}: профилей с телефонами добавлено в индекс")
    log.info(f"Индекс телефонов построен: {len(index)} записей")
    return index


def get_phone_index() -> dict:
    global _phone_index_cache, _phone_index_built_at
    cached = load_json(PHONE_INDEX_PATH, None)
    if cached and time.time() - cached.get("built_at", 0) < PHONE_INDEX_REFRESH_SECONDS:
        _phone_index_cache = cached["index"]
        _phone_index_built_at = cached["built_at"]
        return _phone_index_cache
    log.info("Строю индекс телефонов курьеров...")
    index = fetch_driver_phone_index()
    _phone_index_cache = index
    _phone_index_built_at = time.time()
    save_json(PHONE_INDEX_PATH, {"built_at": _phone_index_built_at, "index": index})
    return index


def find_active_point_b(park_key: str, driver_id: str) -> tuple[str | None, str | None]:
    creds = PARKS[park_key]
    url = f"{FLEET_BASE_URL}/v1/parks/orders/list"
    date_to = datetime.now(timezone.utc)
    date_from = date_to - timedelta(hours=ORDERS_LOOKBACK_HOURS)
    body = {
        "query": {
            "park": {
                "id": creds["park_id"],
                "order": {"booked_at": {"from": date_from.isoformat(), "to": date_to.isoformat()}},
            }
        },
        "limit": 500,
    }
    candidates = []
    cursor = None
    for _page in range(1, 21):
        if cursor:
            body["cursor"] = cursor
        data = fleet_post(url, creds, body)
        orders = data.get("orders", [])
        for order in orders:
            if order.get("driver_profile", {}).get("id") != driver_id:
                continue
            if order.get("status") in ACTIVE_STATUS_PRIORITY:
                candidates.append(order)
        cursor = data.get("cursor")
        if not orders or not cursor:
            break

    if not candidates:
        return None, None

    def sort_key(order):
        return (ACTIVE_STATUS_PRIORITY.index(order.get("status")), order.get("booked_at", ""))

    best = sorted(candidates, key=sort_key)[0]
    route_points = best.get("route_points", [])
    if not route_points:
        return None, best.get("status")
    return route_points[-1].get("address"), best.get("status")


def send_message(chat_id: int, text: str, reply_markup: dict | None = None) -> None:
    url = f"{TELEGRAM_BASE_URL}/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    requests.post(url, json=payload, timeout=15)


def reply_with_point_b(chat_id: int, driver: dict) -> None:
    address, status = find_active_point_b(driver["park"], driver["id"])
    if not address:
        send_message(chat_id, f"Привет, {driver['name']}! Активного заказа с точкой Б сейчас не вижу.", POINT_B_KEYBOARD)
        return
    send_message(chat_id, f"Точка Б: {address}\n(статус заказа: {status})", POINT_B_KEYBOARD)


def handle_message(message: dict) -> None:
    chat_id = message["chat"]["id"]
    from_id = message.get("from", {}).get("id")
    chat_key = str(chat_id)

    contact = message.get("contact")
    if contact is not None:
        if contact.get("user_id") != from_id:
            send_message(chat_id, "Это не твой контакт — нажми кнопку «Поделиться номером» ещё раз.")
            return
        phone = normalize_phone(contact.get("phone_number", ""))
        phone_index = get_phone_index()
        driver = phone_index.get(phone) if phone else None
        if not driver:
            send_message(chat_id, "Не нашёл курьера с таким номером в базе. Напиши Андрею.")
            return
        with _lock:
            verified = get_verified()
            verified[chat_key] = driver
            save_verified(verified)
        send_message(chat_id, f"Готово, {driver['name']}! Дальше жми кнопку «{POINT_B_BUTTON_TEXT}» внизу, когда нужен адрес.")
        reply_with_point_b(chat_id, driver)
        return

    verified = get_verified()
    driver = verified.get(chat_key)
    if driver:
        reply_with_point_b(chat_id, driver)
        return

    send_message(
        chat_id,
        "Привет! Чтобы получать точку Б, подтверди свой номер кнопкой ниже — это одноразово.",
        SHARE_CONTACT_KEYBOARD,
    )


app = Flask(__name__)


@app.route("/", methods=["GET"])
def health():
    return "ok"


def process_message_safely(message: dict) -> None:
    try:
        handle_message(message)
    except Exception as e:
        log.exception(f"Ошибка обработки: {e}")
        try:
            send_message(message["chat"]["id"], "Что-то пошло не так, попробуй ещё раз чуть позже.")
        except Exception:
            pass


@app.route("/webhook", methods=["POST"])
def webhook():
    # Telegram ждёт быстрый ответ на сам вебхук (иначе таймаут и сообщение
    # считается недоставленным) — сборка индекса телефонов при холодном
    # кэше может занимать минуты, поэтому обработка уходит в фон, а сюда
    # отвечаем "ok" сразу же.
    update = request.get_json(force=True, silent=True) or {}
    message = update.get("message")
    if message:
        log.info(f"Сообщение от {message['chat']['id']}: {message.get('text') or '[contact]'}")
        threading.Thread(target=process_message_safely, args=(message,), daemon=True).start()
    return "ok"


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
