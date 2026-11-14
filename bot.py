import asyncio
import datetime
import logging
import os
import random
import sqlite3
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiohttp import ClientSession, web
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN")
ADMIN_ID_env = os.getenv("ADMIN_ID")
ADMIN_ID = int(ADMIN_ID_env) if ADMIN_ID_env and ADMIN_ID_env.isdigit() else 0

if not BOT_TOKEN:
  raise ValueError("❌ BOT_TOKEN не найден!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# === ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ SQLite ===
def init_db():
  conn = sqlite3.connect("bot_database.db")
  cursor = conn.cursor()
  cursor.execute(
      """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            service_name TEXT,
            total_price REAL,
            status TEXT,
            created_at TEXT,
            target_info TEXT
        )
    """
  )
  cursor.execute(
      """
        CREATE TABLE IF NOT EXISTS banned_users (
            user_id INTEGER PRIMARY KEY
        )
    """
  )
  conn.commit()
  conn.close()


def add_order_to_db(
    user_id: int, service_name: str, total_price: float, status: str, target_info: str
):
  conn = sqlite3.connect("bot_database.db")
  cursor = conn.cursor()
  date_str = datetime.datetime.now().strftime("%d %B")
  cursor.execute(
      """
        INSERT INTO orders (user_id, service_name, total_price, status, created_at, target_info)
        VALUES (?, ?, ?, ?, ?, ?)
    """,
      (user_id, service_name, total_price, status, date_str, target_info),
  )
  conn.commit()
  order_id = cursor.lastrowid
  conn.close()
  return order_id


def get_user_orders_from_db(user_id: int):
  conn = sqlite3.connect("bot_database.db")
  cursor = conn.cursor()
  cursor.execute(
      """
        SELECT id, service_name, total_price, status, created_at, target_info 
        FROM orders WHERE user_id = ? ORDER BY id DESC
    """,
      (user_id,),
  )
  rows = cursor.fetchall()
  conn.close()
  orders = []
  for row in rows:
    orders.append(
        {
            "id": row[0],
            "service_name": row[1],
            "total_price": row[2],
            "status": row[3],
            "created_at": row[4],
            "target_info": row[5],
        }
    )
  return orders


def is_user_banned(user_id: int) -> bool:
  conn = sqlite3.connect("bot_database.db")
  cursor = conn.cursor()
  cursor.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
  row = cursor.fetchone()
  conn.close()
  return row is not None


def ban_user_in_db(user_id: int):
  conn = sqlite3.connect("bot_database.db")
  cursor = conn.cursor()
  cursor.execute("INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)", (user_id,))
  conn.commit()
  conn.close()


class OrderForm(StatesGroup):
  service = State()
  target_username = State()
  receipt = State()


class SupportForm(StatesGroup):
  message_text = State()


class VacancyForm(StatesGroup):
  skills = State()
  experience = State()
  salary = State()
  username = State()


# Словари и базы в памяти для отслеживания активности и реквизитов
KNOWN_USERS = {}
USER_MAPPING = {}      # Кэш для связи username <-> user_id для команды /ban
ABANDONED_CHECKOUTS = {}
CARD_DETAILS = []      # Список реквизитов карт/телефонов
CARD_POINTERS = {}     # Индексы для циклического чередования реквизитов по странам

router = Router()


def track_user(user):
  if user:
    USER_MAPPING[user.id] = user.username
    if user.username:
      USER_MAPPING[user.username.lower().lstrip('@')] = user.id


# === СЛОВАРЬ ВАЛЮТ ДЛЯ СТРАН ===
CURRENCY_CONFIG = {
    "RUB": {"country": "Россия", "name": "Рубли (RUB)", "symbol": "₽"},
    "BYN": {"country": "Беларусь", "name": "BYN", "symbol": "BYN"},
    "UAH": {"country": "Украина", "name": "Гривны (UAH)", "symbol": "грн."},
    "KZT": {"country": "Казахстан", "name": "Тенге (KZT)", "symbol": "₸"},
}

# === КРИПТОСЕТИ И РЕКВИЗИТЫ ===
CRYPTO_NETWORKS = {
    "trc20": {
        "name": "USDT (TRC-20)",
        "address": "TYd1ekJgTHnFfgAVYXsi7689GaG5oZN3Xe",
        "network": "TRC-20",
    },
    "bep20": {
        "name": "USDT (BEP-20)",
        "address": "0x0A6C6634f8FD920a5E9E0d241CD46d6C15377f39",
        "network": "BEP-20",
    },
    "ton": {
        "name": "TON",
        "address": "UQDwk7PZoWr0zHv-BItBEGxnq4uyj81z6BlIw74heDgF-Shd",
        "network": "TON",
    },
    "btc": {
        "name": "Bitcoin (BTC)",
        "address": "bc1qj7xy0645vcza7l3et5s8nuahllua9a439c0795",
        "network": "Bitcoin",
    },
}


# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ КАРТ И ЧЕРЕДОВАНИЯ (ROUND-ROBIN) ===
def get_next_card(country: str):
  now = datetime.datetime.now()
  active = []
  for card in CARD_DETAILS:
    if card["country"].lower() == country.lower():
      if card["frozen_until"] and card["frozen_until"] > now:
        continue
      elif card["frozen_until"] and card["frozen_until"] <= now:
        card["frozen_until"] = None
      active.append(card)

  if not active:
    return None

  if country not in CARD_POINTERS:
    CARD_POINTERS[country] = 0

  index = CARD_POINTERS[country] % len(active)
  chosen_card = active[index]
  CARD_POINTERS[country] = (index + 1) % len(active)

  return chosen_card


async def get_exchange_rate(target_currency: str) -> float:
  url = "https://open.er-api.com/v6/latest/USD"
  try:
    async with ClientSession() as session:
      async with session.get(url, timeout=5) as response:
        if response.status == 200:
          data = await response.json()
          rates = data.get("rates", {})
          if target_currency in rates:
            return float(rates[target_currency])
  except Exception as e:
    logger.warning(f"Не удалось получить курс для {target_currency}: {e}")

  fallback_rates = {
      "RUB": 95.0, "BYN": 3.3, "KZT": 480.0, "UAH": 41.5,
  }
  return fallback_rates.get(target_currency, 1.0)


# === АВТОРАССЫЛКА СКИДКИ ===
def get_dynamic_discount_content():
  target_date = datetime.date.today() + datetime.timedelta(days=3)
  target_day = target_date.day
  target_month = target_date.month

  months_ru = [
      "", "января", "февраля", "марта", "апреля", "мая", "июня",
      "июля", "августа", "сентября", "октября", "ноября", "декабря"
  ]
  month_name = months_ru[target_month]

  text = (
      "✨ <b>Специальное предложение</b>\n\n"
      f"Напоминаем, что в нашем сервисе действует персональная скидка <b>20%</b>, которая актуальна до <b>{target_day} {month_name}</b>.\n\n"
      "Будем рады помочь с решением вашей задачи на лучших условиях. Гарантируем конфиденциальность.\n\n"
      "Ждем вас в боте 👇"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="✨ Оформить со скидкой 20%", callback_data="catalog_services")],
          [InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]
      ]
  )
  return text, keyboard


async def scheduled_broadcast(bot: Bot):
  while True:
    await asyncio.sleep(3600)
    now = datetime.datetime.now()
    for user_id, last_time in list(KNOWN_USERS.items()):
      if is_user_banned(user_id):
        continue
      if last_time is None or (now - last_time).total_seconds() >= 24 * 3600:
        try:
          text, keyboard = get_dynamic_discount_content()
          await bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard, parse_mode="HTML")
          KNOWN_USERS[user_id] = now
        except Exception as e:
          logger.error(f"Не удалось отправить автосообщение пользователю {user_id}: {e}")


async def delayed_abandoned_cart_reminder(bot: Bot, user_id: int):
  try:
    await asyncio.sleep(1800)
    if is_user_banned(user_id):
      return
    if user_id in ABANDONED_CHECKOUTS and ABANDONED_CHECKOUTS[user_id]:
      reminder_text = (
          "👋 <b>Завершить оформление?</b>\n\n"
          "Мы заметили, что вы остановились на шаге оплаты. Если у вас возникли вопросы по переводу — поддержка на связи.\n\n"
          "Вернуться к оформлению можно в один клик 👇"
      )
      keyboard = InlineKeyboardMarkup(
          inline_keyboard=[
              [InlineKeyboardButton(text="🛒 Вернуться к заказу", callback_data="catalog_services")],
              [InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="contact_admin")]
          ]
      )
      await bot.send_message(chat_id=user_id, text=reminder_text, reply_markup=keyboard, parse_mode="HTML")
      ABANDONED_CHECKOUTS.pop(user_id, None)
  except asyncio.CancelledError:
    pass


# === РЕПУТАЦИЯ ===
def get_reputation_text_and_keyboard():
  current_date = datetime.date.today()
  base_date = datetime.date(2026, 1, 1)
  days_passed = max(0, (current_date - base_date).days)
  completed = min(3752 + days_passed * 25, 20000)
  
  rng = random.Random(current_date.toordinal())
  in_progress = rng.randint(170, 500)
  new_requests = rng.randint(30, 80)

  text = (
      "📊 <b>Репутация магазина</b>\n\n"
      f"• Успешно выполнено задач: <b>{completed:,}</b>\n"
      f"• В активной работе: <b>{in_progress}</b>\n"
      f"• Новых заявок сегодня: <b>{new_requests}</b>\n\n"
      "<i>Мы дорожим конфиденциальностью каждого клиента и поддерживаем безупречный уровень сервиса.</i>"
  )
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]])
  return text, keyboard


# === ГЛАВНОЕ МЕНЮ ===
def get_main_menu_text():
  return (
      "🧑‍💻 <b>ARENA SHOP — КИБЕР УСЛУГИ</b>\n\n"
      "Здесь вы можете пробить цель: ФИО, номер, адрес и др. "
      "А также заказать взлом соц. сетей и снос аккаунтов. "
      "Все конфиденциально и главное — безопасно. "
      "Работаем 24/7.\n\n"
      "Да прибудет АРЕНА!"
  )


def get_main_menu_keyboard():
  return InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🛒 Заказать", callback_data="catalog_services")],
          [
              InlineKeyboardButton(text="📦 В работе", callback_data="my_orders"),
              InlineKeyboardButton(text="📊 Репутация", callback_data="about_service"),
          ],
          [InlineKeyboardButton(text="💬 Поддержка", callback_data="contact_admin")],
      ]
  )


def get_back_home_keyboard():
  return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]])


# === АДМИНСКИЕ КОМАНДЫ ДЛЯ УПРАВЛЕНИЯ КАРТАМИ И БАНОМ ===
@router.message(Command("ban"))
async def cmd_ban_user(message: Message):
  if ADMIN_ID and message.from_user.id != ADMIN_ID:
    return
  args = message.text.split(maxsplit=1)
  if len(args) < 2:
    await message.reply(
        "❌ Использование:\n"
        "• По ID: `/ban 123456789`\n"
        "• По юзернейму: `/ban @username`",
        parse_mode="Markdown"
    )
    return

  target = args[1].strip()
  target_user_id = None

  if target.isdigit():
    target_user_id = int(target)
  else:
    clean_username = target.lstrip("@").lower()
    target_user_id = USER_MAPPING.get(clean_username)

  if not target_user_id:
    await message.reply("❌ Пользователь не найден в памяти бота. Укажите числовой User ID.")
    return

  ban_user_in_db(target_user_id)
  await message.reply(f"✅ Пользователь с ID <code>{target_user_id}</code> успешно забанен.", parse_mode="HTML")


@router.message(Command("add"))
async def cmd_add_card(message: Message):
  if ADMIN_ID and message.from_user.id != ADMIN_ID:
    return
  args = message.text.split(maxsplit=1)
  if len(args) < 2:
    await message.reply(
        "❌ Использование:\n"
        "• Для РФ/РК: `/add +783839293 ОзонБанк Россия`\n"
        "• Для УКР/РБ: `/add 6672 7778 1113 8888 ПриватБанк Украина`",
        parse_mode="Markdown"
    )
    return

  parts = args[1].split()
  if len(parts) < 3:
    await message.reply("❌ Неверный формат. Укажите реквизиты, банк и страну.")
    return

  country = parts[-1]
  bank = parts[-2]
  details = " ".join(parts[:-2])

  valid_countries = ["Россия", "Казахстан", "Украина", "Беларусь"]
  if country not in valid_countries:
    await message.reply(f"❌ Неверная страна. Доступные: {', '.join(valid_countries)}")
    return

  for card in CARD_DETAILS:
    if card["details"] == details and card["bank"] == bank and card["country"] == country:
      await message.reply("⚠️ Такие реквизиты уже существуют в базе.")
      return

  CARD_DETAILS.append({
      "details": details,
      "bank": bank,
      "country": country,
      "frozen_until": None
  })
  await message.reply(
      f"✅ <b>Реквизиты успешно добавлены!</b>\n\n"
      f"🌍 Страна: {country}\n"
      f"🏦 Банк: {bank}\n"
      f"💳 Данные: <code>{details}</code>",
      parse_mode="HTML"
  )


@router.message(Command("delete"))
async def cmd_delete_card(message: Message):
  if ADMIN_ID and message.from_user.id != ADMIN_ID:
    return
    
  global CARD_DETAILS
  
  args = message.text.split(maxsplit=1)
  if len(args) < 2:
    await message.reply("❌ Использование: `/delete <реквизиты> <Банк> <Страна>`", parse_mode="Markdown")
    return

  parts = args[1].split()
  if len(parts) < 3:
    await message.reply("❌ Неверный формат.")
    return

  country = parts[-1]
  bank = parts[-2]
  details = " ".join(parts[:-2])

  initial_len = len(CARD_DETAILS)
  CARD_DETAILS = [
      c for c in CARD_DETAILS 
      if not (c["details"] == details and c["bank"] == bank and c["country"].lower() == country.lower())
  ]

  if len(CARD_DETAILS) < initial_len:
    await message.reply("✅ Реквизиты успешно удалены.")
  else:
    await message.reply("❌ Реквизиты не найдены в базе.")


@router.message(Command("freeze"))
async def cmd_freeze_card(message: Message):
  if ADMIN_ID and message.from_user.id != ADMIN_ID:
    return
  args = message.text.split(maxsplit=1)
  if len(args) < 2:
    await message.reply("❌ Использование: `/freeze <реквизиты> <Банк> <Страна>`", parse_mode="Markdown")
    return

  parts = args[1].split()
  if len(parts) < 3:
    await message.reply("❌ Неверный формат.")
    return

  country = parts[-1]
  bank = parts[-2]
  details = " ".join(parts[:-2])

  found = False
  now = datetime.datetime.now()
  for card in CARD_DETAILS:
    if card["details"] == details and card["bank"] == bank and card["country"].lower() == country.lower():
      card["frozen_until"] = now + datetime.timedelta(hours=24)
      found = True
      break

  if found:
    await message.reply("✅ Реквизиты зафрижены на 24 часа. По истечении времени они автоматически станут актуальными.")
  else:
    await message.reply("❌ Реквизиты не найдены.")


# === CRYPTOBOT ИНВОЙСЫ И ПРОВЕРКА ===
async def create_cryptobot_invoice(amount_usd: float, description: str):
  if not CRYPTO_BOT_TOKEN:
    return "", None

  url = "https://pay.crypt.bot/api/createInvoice"
  headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
  payload = {
      "asset": "USDT",
      "amount": str(amount_usd),
      "description": description,
      "payload": "order_payment",
  }

  for attempt in range(2):
    try:
      async with ClientSession() as session:
        async with session.post(url, json=payload, headers=headers, timeout=10) as resp:
          if resp.status == 200:
            data = await resp.json()
            if data.get("ok"):
              res = data["result"]
              return res["pay_url"], res["invoice_id"]
    except Exception as e:
      logger.error(f"Попытка {attempt+1} - Ошибка создания CryptoBot инвойса: {e}")
      await asyncio.sleep(1)
  return "", None


async def check_cryptobot_invoice_status(invoice_id: int) -> bool:
  if not CRYPTO_BOT_TOKEN or not invoice_id:
    return True
  url = "https://pay.crypt.bot/api/getInvoices"
  headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
  params = {"invoice_ids": str(invoice_id)}
  try:
    async with ClientSession() as session:
      async with session.get(url, params=params, headers=headers, timeout=10) as resp:
        if resp.status == 200:
          data = await resp.json()
          if data.get("ok"):
            items = data.get("result", {}).get("items", [])
            if items:
              return items[0].get("status") == "paid"
  except Exception as e:
    logger.error(f"Ошибка проверки инвойса CryptoBot: {e}")
  return False


def get_order_details_text(data):
  service_name = data.get("service_name")
  target_username = data.get("target_username")

  details = ""
  if service_name in ["Взлом VK", "Взлом Instagram", "Взлом TikTok"]:
    details += f"• Цель (Ссылка/Юзер): <b>{target_username}</b>\n"
  else:
    details += f"• Юзер цели: <b>{target_username}</b>\n"
  return details


def get_admin_order_details_text(data):
  return get_order_details_text(data)


# === ОБРАБОТЧИКИ КОМАНД И МЕНЮ ===
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
  track_user(message.from_user)
  user_id = message.from_user.id
  if is_user_banned(user_id):
    await message.answer("❌ Вы заблокированы в этом боте.")
    return

  await state.clear()
  if user_id not in KNOWN_USERS:
    KNOWN_USERS[user_id] = datetime.datetime.now()

  await message.answer(get_main_menu_text(), reply_markup=get_main_menu_keyboard(), parse_mode="HTML")


@router.message(Command("order"))
async def cmd_order(message: Message, state: FSMContext):
  track_user(message.from_user)
  if is_user_banned(message.from_user.id):
    return

  await state.clear()
  await state.set_state(OrderForm.service)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="💣 Снос Telegram аккаунта — 10$", callback_data="srv_Снос_Telegram_аккаунта_10")],
          [InlineKeyboardButton(text="🔍 Доксинг (поиск информации) — 20$", callback_data="srv_Доксинг_(Поиск_полной_инфо)_20")],
          [InlineKeyboardButton(text="🔓 Взлом VK — 40$", callback_data="srv_Взлом_VK_40")],
          [InlineKeyboardButton(text="📸 Взлом Instagram — 60$", callback_data="srv_Взлом_Instagram_60")],
          [InlineKeyboardButton(text="🎵 Взлом TikTok — 80$", callback_data="srv_Взлом_TikTok_80")],
          [InlineKeyboardButton(text="💬 Взлом Telegram аккаунта — 100$", callback_data="srv_Взлом_Telegram_аккаунта_100")],
          [InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")],
      ]
  )
  await message.answer(
      "⚡️ <b>Каталог услуг (Заказать)</b>\n\nВыберите подходящий вариант из списка ниже:",
      reply_markup=keyboard,
      parse_mode="HTML",
  )


@router.message(Command("my_orders"))
async def cmd_my_orders(message: Message, state: FSMContext):
  track_user(message.from_user)
  if is_user_banned(message.from_user.id):
    return

  await state.clear()
  user_id = message.from_user.id
  orders = get_user_orders_from_db(user_id)

  if not orders:
    text = "📦 <b>В работе</b>\n\nУ вас пока нет оформленных заявок. Вы можете создать первую через меню «Заказать»."
  else:
    text = "📦 <b>В работе:</b>\n\n"
    for ord_data in orders:
      status_badge = "На рассмотрении" if "Ожидает" in ord_data["status"] else ord_data["status"]
      text += (
          f"📋 <b>Заявка №{ord_data['id']}</b> — <i>{status_badge}</i>\n"
          f"📅 Создана: {ord_data['created_at']}\n"
          f"• Услуга: <b>{ord_data['service_name']}</b>\n"
          f"• Сумма: <b>{ord_data['total_price']}$</b>\n"
          f"• Статус: {ord_data['status']}\n\n"
      )
    text += "<i>По вопросам заявки можно обратиться в поддержку.</i>"

  await message.answer(text, reply_markup=get_back_home_keyboard(), parse_mode="HTML")


@router.message(Command("reputation"))
async def cmd_reputation(message: Message, state: FSMContext):
  track_user(message.from_user)
  if is_user_banned(message.from_user.id):
    return

  await state.clear()
  text, keyboard = get_reputation_text_and_keyboard()
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext):
  track_user(message.from_user)
  if is_user_banned(message.from_user.id):
    return

  await state.set_state(SupportForm.message_text)
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отменить", callback_data="back_home")]])
  text = (
      "💬 <b>Служба поддержки</b>\n\n"
      "Опишите вашу проблему подробно. Примеры вопросов:\n"
      "• <i>Проблема с оплатой (не прошел платеж / как перевести)</i>\n"
      "• <i>Статус выполнения моего заказа</i>\n"
      "• <i>Вопрос по условиям оказания услуг</i>\n\n"
      "<i>Вы также можете прикрепить скриншот или фото к сообщению.</i>"
  )
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(Command("vacancy"))
async def cmd_vacancy(message: Message, state: FSMContext):
  track_user(message.from_user)
  if is_user_banned(message.from_user.id):
    return
  await start_vacancy_process(message, state)


@router.callback_query(F.data == "back_home")
async def back_home(callback: CallbackQuery, state: FSMContext):
  track_user(callback.from_user)
  if is_user_banned(callback.from_user.id):
    await callback.answer("❌ Вы заблокированы.", show_alert=True)
    return

  user_id = callback.from_user.id
  ABANDONED_CHECKOUTS.pop(user_id, None)

  await state.clear()
  await callback.message.edit_text(get_main_menu_text(), reply_markup=get_main_menu_keyboard(), parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "about_service")
async def show_about_service(callback: CallbackQuery):
  track_user(callback.from_user)
  if is_user_banned(callback.from_user.id):
    await callback.answer("❌ Вы заблокированы.", show_alert=True)
    return

  text, keyboard = get_reputation_text_and_keyboard()
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "my_orders")
async def show_my_orders(callback: CallbackQuery):
  track_user(callback.from_user)
  if is_user_banned(callback.from_user.id):
    await callback.answer("❌ Вы заблокированы.", show_alert=True)
    return

  user_id = callback.from_user.id
  orders = get_user_orders_from_db(user_id)

  if not orders:
    text = "📦 <b>В работе</b>\n\nУ вас пока нет оформленных заявок. Вы можете создать первую через меню «Заказать»."
  else:
    text = "📦 <b>В работе:</b>\n\n"
    for ord_data in orders:
      status_badge = "На рассмотрении" if "Ожидает" in ord_data["status"] else ord_data["status"]
      text += (
          f"📋 <b>Заявка №{ord_data['id']}</b> — <i>{status_badge}</i>\n"
          f"📅 Создана: {ord_data['created_at']}\n"
          f"• Услуга: <b>{ord_data['service_name']}</b>\n"
          f"• Сумма: <b>{ord_data['total_price']}$</b>\n"
          f"• Статус: {ord_data['status']}\n\n"
      )
    text += "<i>По вопросам заявки можно обратиться в поддержку.</i>"

  await callback.message.edit_text(text, reply_markup=get_back_home_keyboard(), parse_mode="HTML")
  await callback.answer()


# === ПОДДЕРЖКА И АДМИН-ОТВЕТЫ ===
@router.callback_query(F.data == "contact_admin")
async def contact_admin_info(callback: CallbackQuery, state: FSMContext):
  track_user(callback.from_user)
  if is_user_banned(callback.from_user.id):
    await callback.answer("❌ Вы заблокированы.", show_alert=True)
    return

  await state.set_state(SupportForm.message_text)
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отменить", callback_data="back_home")]])
  text = (
      "💬 <b>Служба поддержки</b>\n\n"
      "Опишите вашу проблему подробно. Примеры вопросов:\n"
      "• <i>Проблема с оплатой (не прошел платеж / как перевести)</i>\n"
      "• <i>Статус выполнения моего заказа</i>\n"
      "• <i>Вопрос по условиям оказания услуг</i>\n\n"
      "<i>Вы также можете прикрепить скриншот или фото к сообщению.</i>"
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.message(SupportForm.message_text)
async def process_support_message(message: Message, state: FSMContext):
  track_user(message.from_user)
  if is_user_banned(message.from_user.id):
    return

  user = message.from_user
  text_content = message.text or message.caption or "Без текста"
  photo_id = message.photo[-1].file_id if message.photo else None

  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  if user.full_name:
    user_info = f"{user.full_name} ({user_info})"

  admin_text = (
      f"📩 <b>Новое обращение в поддержку</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Вопрос: {text_content}\n\n"
      f"💬 <i>Для ответа просто сделайте Reply (ответ) на это сообщение.</i>"
  )

  if ADMIN_ID:
    try:
      if photo_id:
        await message.bot.send_photo(chat_id=ADMIN_ID, photo=photo_id, caption=admin_text, parse_mode="HTML")
      else:
        await message.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML")
    except Exception as e:
      logger.error(f"Не удалось отправить обращение админу: {e}")

  success_text = "✅ <b>Мы получили ваше обращение</b>\n\nСпециалист ответит вам в ближайшее время."
  await message.answer(success_text, reply_markup=get_back_home_keyboard(), parse_mode="HTML")
  await state.clear()


# === ВАКАНСИИ (ЗАПОЛНЕНИЕ ЗАЯВКИ В 4 ШАГА) ===
@router.callback_query(F.data == "vacancy_start")
async def vacancy_callback(callback: CallbackQuery, state: FSMContext):
  track_user(callback.from_user)
  if is_user_banned(callback.from_user.id):
    await callback.answer("❌ Вы заблокированы.", show_alert=True)
    return
  await start_vacancy_process(callback.message, state, edit=True)
  await callback.answer()


async def start_vacancy_process(message: Message, state: FSMContext, edit: bool = False):
  await state.clear()
  await state.set_state(VacancyForm.skills)
  
  text = (
      "💼 <b>Заполнение заявки на вакансию (Шаг 1/4)</b>\n\n"
      "Что вы умеете? Расскажите о своих навыках.\n"
      "<i>Примеры направлений:</i>\n"
      "• Снос аккаунтов\n"
      "• Хакнуть Telegram / Instagram / VK / TikTok\n"
      "• Доксинг / Деанон\n\n"
      "Напишите текстовое описание ваших умений:"
  )
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отменить", callback_data="back_home")]])
  
  if edit:
    await message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  else:
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(VacancyForm.skills, F.text)
async def process_vacancy_skills(message: Message, state: FSMContext):
  track_user(message.from_user)
  if is_user_banned(message.from_user.id):
    return

  await state.update_data(vacancy_skills=message.text.strip())
  await state.set_state(VacancyForm.experience)
  
  text = (
      "💼 <b>Заполнение заявки на вакансию (Шаг 2/4)</b>\n\n"
      "Укажите ваш опыт работы в данной сфере.\n"
      "<i>Если опыта нет, просто поставьте прочерк (-)</i>"
  )
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отменить", callback_data="back_home")]])
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(VacancyForm.experience, F.text)
async def process_vacancy_experience(message: Message, state: FSMContext):
  track_user(message.from_user)
  if is_user_banned(message.from_user.id):
    return

  await state.update_data(vacancy_experience=message.text.strip())
  await state.set_state(VacancyForm.salary)
  
  text = (
      "💼 <b>Заполнение заявки на вакансию (Шаг 3/4)</b>\n\n"
      "На какую зарплату/условия вы рассчитываете?"
  )
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отменить", callback_data="back_home")]])
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(VacancyForm.salary, F.text)
async def process_vacancy_salary(message: Message, state: FSMContext):
  track_user(message.from_user)
  if is_user_banned(message.from_user.id):
    return

  await state.update_data(vacancy_salary=message.text.strip())
  await state.set_state(VacancyForm.username)
  
  text = (
      "💼 <b>Заполнение заявки на вакансию (Шаг 4/4)</b>\n\n"
      "Укажите ваш юзернейм в Telegram (например, @username):"
  )
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отменить", callback_data="back_home")]])
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(VacancyForm.username, F.text)
async def process_vacancy_final(message: Message, state: FSMContext):
  track_user(message.from_user)
  if is_user_banned(message.from_user.id):
    return

  await state.update_data(vacancy_username=message.text.strip())
  data = await state.get_data()
  user = message.from_user

  skills = data.get("vacancy_skills")
  experience = data.get("vacancy_experience")
  salary = data.get("vacancy_salary")
  tg_username = data.get("vacancy_username")

  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  if user.full_name:
    user_info = f"{user.full_name} ({user_info})"

  admin_vacancy_text = (
      "💼 <b>Новая заявка на вакансию!</b>\n\n"
      f"• Отправитель: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• 1. Что умеет: {skills}\n"
      f"• 2. Опыт работы: {experience}\n"
      f"• 3. Ожидания по з/п: {salary}\n"
      f"• 4. Юзернейм для связи: {tg_username}\n\n"
      "💬 <i>Сделайте Reply на это сообщение для связи с кандидатом.</i>"
  )

  if ADMIN_ID:
    try:
      await message.bot.send_message(chat_id=ADMIN_ID, text=admin_vacancy_text, parse_mode="HTML")
    except Exception as e:
      logger.error(f"Не удалось отправить заявку на вакансию админу: {e}")

  success_text = (
      "✅ <b>Заявка успешно отправлена!</b>\n\n"
      "Мы рассмотрим вашу анкету и свяжемся с вами в ближайшее время, если ваша кандидатура нам подойдет."
  )
  await message.answer(success_text, reply_markup=get_back_home_keyboard(), parse_mode="HTML")
  await state.clear()


@router.message(F.reply_to_message)
async def admin_reply_handler(message: Message):
  if not ADMIN_ID or message.from_user.id != ADMIN_ID:
    return

  replied_msg = message.reply_to_message
  if not replied_msg:
    return

  text_to_parse = replied_msg.text or replied_msg.caption or ""
  target_user_id = None

  for line in text_to_parse.split("\n"):
    if "User ID:" in line:
      parts = line.split("<code>")
      if len(parts) > 1:
        id_str = parts[1].split("</code>")[0].strip()
        if id_str.isdigit():
          target_user_id = int(id_str)

  if not target_user_id:
    for word in text_to_parse.split():
      if word.isdigit() and len(word) > 5:
        target_user_id = int(word)
        break

  if not target_user_id:
    await message.reply("❌ Не удалось распознать ID пользователя из выбранного сообщения.")
    return

  try:
    answer_text = f"💬 <b>Ответ от поддержки/администрации:</b>\n\n{message.text or message.caption or ''}"
    await message.bot.send_message(chat_id=target_user_id, text=answer_text, parse_mode="HTML")
    await message.reply("✅ Ответ успешно отправлен пользователю.")
  except Exception as e:
    await message.reply(f"❌ Не удалось отправить сообщение: {e}")


# === ЦИФРОВОЙ КАТАЛОГ УСЛУГ ===
@router.callback_query(F.data.in_(["catalog_services", "start_order"]))
async def process_catalog_services(callback: CallbackQuery, state: FSMContext):
  track_user(callback.from_user)
  if is_user_banned(callback.from_user.id):
    await callback.answer("❌ Вы заблокированы.", show_alert=True)
    return

  user_id = callback.from_user.id
  ABANDONED_CHECKOUTS.pop(user_id, None)

  await state.clear()
  await state.set_state(OrderForm.service)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="💣 Снос Telegram аккаунта — 10$", callback_data="srv_Снос_Telegram_аккаунта_10")],
          [InlineKeyboardButton(text="🔍 Доксинг (поиск информации) — 20$", callback_data="srv_Доксинг_(Поиск_полной_инфо)_20")],
          [InlineKeyboardButton(text="🔓 Взлом VK — 40$", callback_data="srv_Взлом_VK_40")],
          [InlineKeyboardButton(text="📸 Взлом Instagram — 60$", callback_data="srv_Взлом_Instagram_60")],
          [InlineKeyboardButton(text="🎵 Взлом TikTok — 80$", callback_data="srv_Взлом_TikTok_80")],
          [InlineKeyboardButton(text="💬 Взлом Telegram аккаунта — 100$", callback_data="srv_Взлом_Telegram_аккаунта_100")],
          [InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")],
      ]
  )
  await callback.message.edit_text(
      "⚡️ <b>Каталог услуг (Заказать)</b>\n\nВыберите подходящий вариант из списка ниже:",
      reply_markup=keyboard,
      parse_mode="HTML",
  )
  await callback.answer()


@router.callback_query(OrderForm.service, F.data.startswith("srv_"))
async def process_service(callback: CallbackQuery, state: FSMContext):
  track_user(callback.from_user)
  if is_user_banned(callback.from_user.id):
    await callback.answer("❌ Вы заблокированы.", show_alert=True)
    return

  parts = callback.data.split("_")
  price = int(parts[-1])
  service_name = " ".join(parts[1:-1]).replace("_", " ")

  await state.update_data(service_name=service_name, service_price=price, total_price=price)
  await state.set_state(OrderForm.target_username)

  if service_name in ["Взлом VK", "Взлом Instagram", "Взлом TikTok"]:
    prompt_text = f"<b>Услуга: {service_name} ({price}$)</b>\n\nВведите юзер или ссылку на профиль цели:"
  else:
    prompt_text = f"<b>Услуга: {service_name} ({price}$)</b>\n\nВведите юзернейм Telegram аккаунта цели (например, @username):"

  await callback.message.edit_text(prompt_text, parse_mode="HTML")
  await callback.answer()


@router.message(OrderForm.target_username, F.text)
async def process_target_username(message: Message, state: FSMContext):
  track_user(message.from_user)
  if is_user_banned(message.from_user.id):
    return

  target_val = message.text.strip()
  await state.update_data(target_username=target_val, target_phone="-")
  data = await state.get_data()
  service_name = data.get("service_name")
  total_price = data.get("total_price", 0)

  summary_text = (
      "✨ <b>Заявка готова</b>\n\n"
      "📋 <b>Детали вашего заказа:</b>\n"
      f"• Услуга: <b>{service_name}</b>\n"
      f"{get_order_details_text(data)}"
      f"💵 <b>К оплате:</b> <b>{total_price}$</b>\n\n"
      "💳 Перейдите к оплате удобным способом с помощью кнопки ниже 👇"
  )
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Перейти к оплате", callback_data="go_to_payment")]])
  await message.answer(summary_text, reply_markup=keyboard, parse_mode="HTML")


# === ОПЛАТА И ШЛЮЗЫ ===
@router.callback_query(F.data == "go_to_payment")
async def process_go_to_payment(callback: CallbackQuery, state: FSMContext):
  track_user(callback.from_user)
  if is_user_banned(callback.from_user.id):
    await callback.answer("❌ Вы заблокированы.", show_alert=True)
    return

  user_id = callback.from_user.id
  
  if user_id in ABANDONED_CHECKOUTS:
    ABANDONED_CHECKOUTS[user_id].cancel()
  ABANDONED_CHECKOUTS[user_id] = asyncio.create_task(delayed_abandoned_cart_reminder(callback.bot, user_id))

  data = await state.get_data()
  total_price = data.get("total_price", 10)
  service_name = data.get("service_name", "")

  is_restricted = "убийство" in service_name.lower()

  if is_restricted:
    payment_text = (
        "⏳ <b>Бронирование зафиксировано</b>\n"
        "Время на завершение оплаты: <b><u>20:00</u></b>\n\n"
        "⚠️ <b>Внимание:</b> Для выбранной услуги оплата банковскими картами недоступна по соображениям безопасности. Доступна только криптовалюта и CryptoBot.\n\n"
        f"💳 <b>Способ оплаты</b>\n\n"
        f"Сумма: <b>{total_price}$</b>\n\n"
        "Выберите вариант:"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤖 CryptoBot (без комиссий)", callback_data="pay_cryptobot")],
            [InlineKeyboardButton(text="🪙 Криптовалюта (выбор сети)", callback_data="crypto_expert")],
            [InlineKeyboardButton(text="💬 Нужна помощь", callback_data="payment_help_manager")]
        ]
    )
  else:
    payment_text = (
        "⏳ <b>Бронирование зафиксировано</b>\n"
        "Время на завершение оплаты: <b><u>20:00</u></b>\n\n"
        f"💳 <b>Способ оплаты</b>\n\n"
        f"Сумма: <b>{total_price}$</b>\n\n"
        "Выберите вариант валюты для оплаты картой или крипто-шлюзы:"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить в рублях", callback_data="pay_currency_RUB")],
            [InlineKeyboardButton(text="💳 Оплатить в BYN", callback_data="pay_currency_BYN")],
            [InlineKeyboardButton(text="💳 Оплатить в гривнах", callback_data="pay_currency_UAH")],
            [InlineKeyboardButton(text="💳 Оплатить в тенге", callback_data="pay_currency_KZT")],
            [InlineKeyboardButton(text="🤖 CryptoBot (без комиссий)", callback_data="pay_cryptobot")],
            [InlineKeyboardButton(text="🪙 Криптовалюта (выбор сети)", callback_data="crypto_expert")],
            [InlineKeyboardButton(text="💬 Нужна помощь", callback_data="payment_help_manager")]
        ]
    )

  await callback.message.edit_text(payment_text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data.startswith("pay_currency_"))
async def process_pay_currency(callback: CallbackQuery, state: FSMContext):
  track_user(callback.from_user)
  if is_user_banned(callback.from_user.id):
    await callback.answer("❌ Вы заблокированы.", show_alert=True)
    return

  curr_code = callback.data.split("_")[2] # RUB, BYN, UAH, KZT
  config = CURRENCY_CONFIG.get(curr_code, {"country": "Россия", "name": "Рубли", "symbol": "₽"})
  country = config["country"]

  await state.update_data(selected_currency=curr_code, selected_country=country)

  data = await state.get_data()
  total_price = data.get("total_price", 10)
  
  rate = await get_exchange_rate(curr_code)
  local_amount = round(total_price * rate)

  # Получаем реквизит строго по кругу (Round-Robin) для выбранной страны
  card = get_next_card(country)

  if not card:
    text = (
        f"❌ <b>Реквизиты временно недоступны</b>\n\n"
        f"К сожалению, в данный момент реквизиты для оплаты в <b>{config['name']}</b> недоступны.\n\n"
        "Мы уже решаем эту проблему. Пожалуйста, оплатите через <b>CryptoBot</b> или реквизиты криптовалюты 👇"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤖 CryptoBot", callback_data="pay_cryptobot")],
            [InlineKeyboardButton(text="🪙 Криптовалюта", callback_data="crypto_expert")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="go_to_payment")]
        ]
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()
    return

  header_map = {
      "RUB": "Оплата в рублях",
      "KZT": "Оплата в тенге (Kaspi)",
      "UAH": "Оплата в гривнах",
      "BYN": "Оплата в BYN"
  }
  header_title = header_map.get(curr_code, "Оплата по реквизитам")

  text = (
      f"💳 <b>{header_title}</b>\n\n"
      f"• Сумма в долларах: <b>{total_price}$</b>\n"
      f"• Актуальный курс: <b>1 USD = {rate:.2f} {config['symbol']}</b>\n"
      f"• <b>Итого к оплате: {local_amount:,} {config['symbol']}</b>\n\n"
      f"⚠️ <b>Внимание!</b> Переводите средства <b>строго</b> на указанные реквизиты ниже:\n\n"
      f"<b>Банк:</b> {card['bank']}\n"
      f"<b>Реквизиты:</b> <code>{card['details']}</code>\n\n"
      "💡 <i>После успешного перевода нажмите кнопку ниже, чтобы прикрепить чек.</i>"
  )
  keyboard = InlineKeyboardMarkup(inline_keyboard=[
      [InlineKeyboardButton(text="✅ Прикрепить чек", callback_data="ask_for_receipt")],
      [InlineKeyboardButton(text="⬅️ Назад", callback_data="go_to_payment")]
  ])
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "payment_help_manager")
async def process_payment_help_manager(callback: CallbackQuery):
  track_user(callback.from_user)
  if is_user_banned(callback.from_user.id):
    await callback.answer("❌ Вы заблокированы.", show_alert=True)
    return

  if ADMIN_ID:
    try:
      user = callback.from_user
      user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
      await callback.bot.send_message(chat_id=ADMIN_ID, text=f"🚨 Запрос помощи с оплатой от {user_info} (<code>{user.id}</code>)", parse_mode="HTML")
    except Exception:
      pass
  await callback.answer("✅ Менеджер уведомлен, скоро свяжется с вами.", show_alert=True)


@router.callback_query(F.data == "pay_cryptobot")
async def process_pay_cryptobot(callback: CallbackQuery, state: FSMContext):
  track_user(callback.from_user)
  if is_user_banned(callback.from_user.id):
    await callback.answer("❌ Вы заблокированы.", show_alert=True)
    return

  user_id = callback.from_user.id
  ABANDONED_CHECKOUTS.pop(user_id, None)

  data = await state.get_data()
  total_price = data.get("total_price", 10)

  pay_url, inv_id = await create_cryptobot_invoice(total_price, "Оплата услуг")
  if not pay_url:
    await callback.answer("❌ Сервис недоступен.", show_alert=True)
    return

  await state.update_data(invoice_id=inv_id)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🔗 Оплатить счет", url=pay_url)],
          [InlineKeyboardButton(text="✅ Проверить оплату", callback_data="cryptobot_check_success")],
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="go_to_payment")],
      ]
  )
  await callback.message.edit_text(f"🤖 <b>CryptoBot</b>\n\nСумма: <b>{total_price} USDT</b>\n\nПосле оплаты нажмите кнопку проверки:", reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "cryptobot_check_success")
async def process_cryptobot_check(callback: CallbackQuery, state: FSMContext):
  track_user(callback.from_user)
  if is_user_banned(callback.from_user.id):
    await callback.answer("❌ Вы заблокированы.", show_alert=True)
    return

  data = await state.get_data()
  inv_id = data.get("invoice_id")
  
  is_paid = await check_cryptobot_invoice_status(inv_id)
  if not is_paid:
    await callback.answer("⏳ Платеж еще не поступил. Попробуйте через минуту.", show_alert=True)
    return

  user = callback.from_user
  ABANDONED_CHECKOUTS.pop(user.id, None)

  service_name = data.get("service_name", "Услуга")
  total_price = data.get("total_price", 10)
  target_info = f"Юзер: {data.get('target_username')}"

  add_order_to_db(user.id, service_name, total_price, "✅ Оплачено через CryptoBot", target_info)

  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  if user.full_name:
    user_info = f"{user.full_name} ({user_info})"

  if ADMIN_ID:
    try:
      admin_order_text = (
          f"🤖 <b>Успешная оплата CryptoBot</b>\n\n"
          f"• От кого: {user_info}\n"
          f"• User ID: <code>{user.id}</code>\n"
          f"• Услуга: <b>{service_name}</b>\n"
          f"{get_admin_order_details_text(data)}"
          f"• Сумма: <b>{total_price}$</b>"
      )
      await callback.bot.send_message(chat_id=ADMIN_ID, text=admin_order_text, parse_mode="HTML")
    except Exception as e:
      logger.error(f"Ошибка отправки админу: {e}")

  await callback.message.edit_text("✅ <b>Оплата подтверждена!</b>\n\nЗаявка принята в работу.", reply_markup=get_back_home_keyboard(), parse_mode="HTML")
  await state.clear()


@router.callback_query(F.data == "crypto_expert")
async def process_crypto_expert(callback: CallbackQuery, state: FSMContext):
  track_user(callback.from_user)
  if is_user_banned(callback.from_user.id):
    await callback.answer("❌ Вы заблокированы.", show_alert=True)
    return

  user_id = callback.from_user.id
  ABANDONED_CHECKOUTS.pop(user_id, None)

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🔴 USDT (TRC-20)", callback_data="pay_crypto_trc20")],
          [InlineKeyboardButton(text="🟢 USDT (BEP-20)", callback_data="pay_crypto_bep20")],
          [InlineKeyboardButton(text="🔵 TON", callback_data="pay_crypto_ton")],
          [InlineKeyboardButton(text="🟠 Bitcoin (BTC)", callback_data="pay_crypto_btc")],
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="go_to_payment")],
      ]
  )
  await callback.message.edit_text("🪙 Выберите криптовалютную сеть:", reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data.startswith("pay_crypto_"))
async def process_pay_crypto(callback: CallbackQuery, state: FSMContext):
  track_user(callback.from_user)
  if is_user_banned(callback.from_user.id):
    await callback.answer("❌ Вы заблокированы.", show_alert=True)
    return

  net_key = callback.data.split("_")[2]
  net_info = CRYPTO_NETWORKS.get(net_key)
  if not net_info:
    await callback.answer("Сеть не найдена", show_alert=True)
    return

  data = await state.get_data()
  total_price = data.get("total_price", 10)

  text = (
      f"💳 <b>Оплата {net_info['name']}</b>\n\n"
      f"• Сумма: <b>{total_price}$</b>\n"
      f"• Сеть: <b>{net_info['network']}</b>\n"
      f"• Реквизиты:\n<code>{net_info['address']}</code>\n\n"
      "💡 <i>После перевода прикрепите чек.</i>"
  )
  keyboard = InlineKeyboardMarkup(inline_keyboard=[
      [InlineKeyboardButton(text="✅ Прикрепить чек", callback_data="ask_for_receipt")],
      [InlineKeyboardButton(text="⬅️ Назад", callback_data="crypto_expert")]
  ])
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "ask_for_receipt")
async def ask_for_receipt(callback: CallbackQuery, state: FSMContext):
  track_user(callback.from_user)
  if is_user_banned(callback.from_user.id):
    await callback.answer("❌ Вы заблокированы.", show_alert=True)
    return

  await state.set_state(OrderForm.receipt)
  text = "🧾 <b>Подтверждение платежа</b>\n\nОтправьте в чат <b>скриншот или фотографию чека</b> о выполненном переводе для проверки."
  await callback.message.edit_text(text, parse_mode="HTML")
  await callback.answer()


@router.message(OrderForm.receipt, F.photo)
async def process_receipt_photo(message: Message, state: FSMContext):
  track_user(message.from_user)
  if is_user_banned(message.from_user.id):
    return

  user = message.from_user
  ABANDONED_CHECKOUTS.pop(user.id, None)
  data = await state.get_data()

  service_name = data.get("service_name", "Услуга")
  total_price = data.get("total_price", 10)
  target_info = f"Юзер: {data.get('target_username')}"

  add_order_to_db(user.id, service_name, total_price, "⏳ Ожидает проверки чека", target_info)

  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  if user.full_name:
    user_info = f"{user.full_name} ({user_info})"

  admin_order_text = (
      f"🛒 <b>Новая заявка с чеком</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Услуга: <b>{service_name}</b>\n"
      f"{get_admin_order_details_text(data)}"
      f"• Сумма: <b>{total_price}$</b>\n\n"
      f"💬 <i>Чек прикреплен выше. Сделайте Reply для ответа.</i>"
  )
  if ADMIN_ID:
    try:
      photo_id = message.photo[-1].file_id
      await message.bot.send_photo(chat_id=ADMIN_ID, photo=photo_id, caption=admin_order_text, parse_mode="HTML")
    except Exception as e:
      logger.error(f"Не удалось отправить заказ админу: {e}")

  final_text = "⏳ <b>Платёж проверяется</b>\n\nАдминистратор проверяет платеж. Мы свяжемся с вами в течение <b>25 минут</b>."
  await message.answer(final_text, reply_markup=get_back_home_keyboard(), parse_mode="HTML")
  await state.clear()


@router.message(OrderForm.receipt, ~F.photo)
async def process_receipt_invalid(message: Message):
  track_user(message.from_user)
  if is_user_banned(message.from_user.id):
    return
  await message.answer("📎 Пожалуйста, прикрепите <b>скриншот или фото чека изображением</b>. Текстовые сообщения не принимаются.")


# === ВЕБ-СЕРВЕР ДЛЯ RENDER ===
async def handle_ping(request):
  return web.Response(text="Bot is running!")


async def start_web_server():
  app = web.Application()
  app.router.add_get("/", handle_ping)
  runner = web.AppRunner(app)
  await runner.setup()
  port = int(os.getenv("PORT", 10000))
  site = web.TCPSite(runner, "0.0.0.0", port)
  await site.start()


async def main():
  init_db()
  bot = Bot(token=BOT_TOKEN)
  
  commands = [
      BotCommand(command="start", description="Главное меню"),
      BotCommand(command="order", description="Заказать"),
      BotCommand(command="my_orders", description="В работе"),
      BotCommand(command="reputation", description="Репутация"),
      BotCommand(command="support", description="Поддержка"),
  ]
  await bot.set_my_commands(commands)

  dp = Dispatcher()
  dp.include_router(router)

  asyncio.create_task(start_web_server())
  asyncio.create_task(scheduled_broadcast(bot))

  await bot.delete_webhook(drop_pending_updates=True)
  await dp.start_polling(bot)


if __name__ == "__main__":
  try:
    asyncio.run(main())
  except (KeyboardInterrupt, SystemExit):
    logger.info("Бот остановлен пользователем.")
