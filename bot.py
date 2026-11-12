import asyncio
import datetime
import logging
import os
import random
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
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


class OrderForm(StatesGroup):
  service = State()
  target_username = State()
  target_phone = State()
  receipt = State()  # Состояние для ожидания чека


class SupportForm(StatesGroup):
  reason = State()
  photo = State()


# База данных в памяти для хранения заказов пользователей
USER_ORDERS = {}

# Словарь для хранения времени последней авто-рассылки пользователям: {user_id: datetime}
KNOWN_USERS = {}

# Хранилище активных задач таймеров и дожимов для неоплаченных оплат
PAYMENT_TIMERS = {}
ABANDONED_CHECKOUTS = {}

router = Router()


# === СЛОВАРЬ ВАЛЮТ ДЛЯ СТРАН (ДЛЯ ОЦЕНОК) ===
COUNTRY_CURRENCIES = {
    "Украина": {"code": "UAH", "symbol": "грн."},
    "Россия": {"code": "RUB", "symbol": "руб."},
    "Беларусь": {"code": "BYN", "symbol": "byn"},
    "Казахстан": {"code": "KZT", "symbol": "тңг."},
    "Молдова": {"code": "MDL", "symbol": "лей"},
    "Узбекистан": {"code": "UZS", "symbol": "сум"},
    "Кыргызстан": {"code": "KGS", "symbol": "сом"},
    "Таджикистан": {"code": "TJS", "symbol": "сом."},
    "Грузия": {"code": "GEL", "symbol": "lari"},
    "Армения": {"code": "AMD", "symbol": "драм"},
    "Азербайджан": {"code": "AZN", "symbol": "манат"},
}


# === ФУНКЦИЯ ПОЛУЧЕНИЯ РЕАЛЬНЫХ КУРСОВ ВАЛЮТ ===
async def get_exchange_rate(target_currency: str) -> float:
  """Получает актуальный курс USD к целевой валюте из публичных API."""
  url = f"https://open.er-api.com/v6/latest/USD"
  try:
    async with ClientSession() as session:
      async with session.get(url, timeout=5) as response:
        if response.status == 200:
          data = await response.json()
          rates = data.get("rates", {})
          if target_currency in rates:
            return float(rates[target_currency])
  except Exception as e:
    logger.warning(
        f"Не удалось получить курс для {target_currency}: {e}. Используем"
        " резервные значения."
    )

  fallback_rates = {
      "UAH": 41.5,
      "RUB": 95.0,
      "BYN": 3.3,
      "KZT": 480.0,
      "MDL": 18.0,
      "UZS": 12600.0,
      "KGS": 89.0,
      "TJS": 10.9,
      "GEL": 2.7,
      "AMD": 390.0,
      "AZN": 1.7,
  }
  return fallback_rates.get(target_currency, 1.0)


# === ФУНКЦИЯ ГЕНЕРАЦИИ ДИНАМИЧЕСКОЙ ДАТЫ ДЛЯ СКИДКИ ===
def get_dynamic_discount_content():
  target_date = datetime.date.today() + datetime.timedelta(days=3)
  target_day = target_date.day
  target_month = target_date.month

  months_ru = [
      "",
      "января",
      "февраля",
      "марта",
      "апреля",
      "мая",
      "июня",
      "июля",
      "августа",
      "сентября",
      "октября",
      "ноября",
      "декабря",
  ]
  month_name = months_ru[target_month]

  text = (
      "🔥 <b>Специальное предложение!</b> 🔥\n\n"
      f"Напоминаем, что в нашем сервисе действуют скидки <b>-20%</b> только до"
      f" <b>{target_day} {month_name}</b>!\n\n"
      "Успейте оформить заказ на лучших условиях. Мы гарантируем скорость,"
      " качество и полную конфиденциальность.\n\n"
      "Ждем вас в боте 👇"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🔥 Успеть заказать со скидкой -20%",
                  callback_data="start_order",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🏠 На главную", callback_data="back_home"
              )
          ],
      ]
  )
  return text, keyboard


# === ФОНОВАЯ ЗАДАЧА АВТО-РАССЫЛКИ КАЖДЫЕ 24 ЧАСА ===
async def scheduled_broadcast(bot: Bot):
  while True:
    await asyncio.sleep(3600)
    now = datetime.datetime.now()
    for user_id, last_time in list(KNOWN_USERS.items()):
      if (
          last_time is None
          or (now - last_time).total_seconds() >= 24 * 3600
      ):
        try:
          text, keyboard = get_dynamic_discount_content()
          await bot.send_message(
              chat_id=user_id, text=text, reply_markup=keyboard, parse_mode="HTML"
          )
          KNOWN_USERS[user_id] = now
        except Exception as e:
          logger.error(
              f"Не удалось отправить автосообщение пользователю {user_id}: {e}"
          )


# === ФОНОВАЯ ЗАДАЧА ДОЖИМА ЛИДОВ ЧЕРЕЗ 30 МИНУТ ===
async def delayed_abandoned_cart_reminder(bot: Bot, user_id: int):
  try:
    await asyncio.sleep(1800)
    if user_id in ABANDONED_CHECKOUTS and ABANDONED_CHECKOUTS[user_id]:
      reminder_text = (
          "👋 <b>Завершите оформление заказа!</b>\n\n"
          "Мы заметили, что вы остановились на этапе оплаты, но так и не завершили заказ.\n"
          "Возникли вопросы или трудности с оплатой? Наша поддержка всегда на связи, а оформление займет всего минуту!\n\n"
          "Нажмите кнопку ниже, чтобы вернуться в бот 👇"
      )
      keyboard = InlineKeyboardMarkup(
          inline_keyboard=[
              [InlineKeyboardButton(text="🛒 Вернуться к заказу", callback_data="start_order")],
              [InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="contact_admin")]
          ]
      )
      await bot.send_message(chat_id=user_id, text=reminder_text, reply_markup=keyboard, parse_mode="HTML")
      ABANDONED_CHECKOUTS.pop(user_id, None)
  except asyncio.CancelledError:
    pass
  except Exception as e:
    logger.error(f"Ошибка отправки напоминания о дожиме для {user_id}: {e}")


# === ФУНКЦИЯ ЖИВОГО ТАЙМЕРА ОБРАТНОГО ОТСЧЕТА (КАЖДУЮ СЕКУНДУ) ===
async def run_payment_timer(bot: Bot, chat_id: int, message_id: int, base_text: str, keyboard_buttons_factory):
  total_seconds = 20 * 60
  task_key = (chat_id, message_id)
  
  try:
    while total_seconds > 0:
      mins, secs = divmod(total_seconds, 60)
      timer_str = f"{mins:02d}:{secs:02d}"
      
      current_text = (
          f"⏳ <b>Успейте оплатить! До окончания бронирования осталось: <u>{timer_str}</u></b>\n\n"
          f"{base_text}"
      )
      keyboard = keyboard_buttons_factory(timer_active=True, time_left=timer_str)
      
      try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=current_text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
      except Exception:
        pass
      
      await asyncio.sleep(1)
      total_seconds -= 1
      
    expired_text = (
        "❌ <b>Время на оплату заказа истекло!</b>\n\n"
        "Бронь заказа была отменена. Пожалуйста, оформите заказ заново."
    )
    expired_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🛒 Сделать новый заказ", callback_data="start_order")]]
    )
    try:
      await bot.edit_message_text(
          chat_id=chat_id,
          message_id=message_id,
          text=expired_text,
          reply_markup=expired_kb,
          parse_mode="HTML"
      )
    except Exception:
      pass
      
    if task_key in PAYMENT_TIMERS:
      del PAYMENT_TIMERS[task_key]
      
  except asyncio.CancelledError:
    pass


# === ФУНКЦИЯ СОЗДАНИЯ ИНВОЙСА CRYPTOBOT ===
async def create_cryptobot_invoice(amount_usd: float, description: str) -> str:
  if not CRYPTO_BOT_TOKEN:
    return ""

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
              return data["result"]["pay_url"]
    except Exception as e:
      logger.error(f"Попытка {attempt+1} - Ошибка создания CryptoBot инвойса: {e}")
      await asyncio.sleep(1)
  return ""


# === ФУНКЦИЯ ДЛЯ ГЕНЕРАЦИИ РЕПУТАЦИИ ===
async def get_reputation_text_and_keyboard():
  current_date = datetime.date.today()
  base_date = datetime.date(2026, 1, 1)
  days_passed = max(0, (current_date - base_date).days)

  completed = min(3752 + days_passed * 25, 20000)

  rng = random.Random(current_date.toordinal())
  in_progress = rng.randint(170, 500)
  new_requests = rng.randint(30, 80)

  text = (
      "📊 <b>Наша статистика и репутация:</b>\n\n"
      f"• Выполненных заказов: <b>{completed}</b>\n"
      f"• Заказов в работе: <b>{in_progress}</b>\n"
      f"• Новых заявок: <b>{new_requests}</b>\n\n"
      "<i>Мы дорожим своей репутацией и гарантируем полную конфиденциальность.</i>"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_home")]
      ]
  )
  return text, keyboard


# === 0. ГЛАВНОЕ МЕНЮ И КОМАНДЫ ===
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
  await state.clear()
  user_id = message.from_user.id
  if user_id not in KNOWN_USERS:
    KNOWN_USERS[user_id] = datetime.datetime.now()

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🛒 Сделать заказ", callback_data="start_order"
              )
          ],
          [
              InlineKeyboardButton(
                  text="📦 Мои заказы", callback_data="my_orders"
              ),
              InlineKeyboardButton(
                  text="📊 Наша репутация", callback_data="reputation"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="💬 Поддержка",
                  callback_data="contact_admin",
              )
          ],
      ]
  )
  text = (
      "<b>Добро пожаловать в сервис «КИБЕР УСЛУГИ»!</b> 💻\n\n"
      "Мы предоставляем профессиональный спектр киберуслуг и решение ваших"
      " задач быстро, качественно и конфиденциально.\n\n"
      "Выберите нужный пункт в меню ниже:"
  )
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(Command("reputation"))
async def cmd_reputation(message: Message):
  text, keyboard = await get_reputation_text_and_keyboard()
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(Command("my_orders"))
async def cmd_my_orders(message: Message):
  user_id = message.from_user.id
  orders = USER_ORDERS.get(user_id, [])

  if not orders:
    text = (
        "📦 <b>Мои заказы</b>\n\nУ вас пока нет активных или завершенных"
        " заказов."
    )
  else:
    text = "📦 <b>Ваши заказы:</b>\n\n"
    for idx, ord_data in enumerate(orders, 1):
      text += (
          f"<b>Заказ #{idx}</b>\n• Услуга: {ord_data['service_name']}\n• Сумма:"
          f" {ord_data['total_price']}$\n• Статус: {ord_data['status']}\n\n"
      )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]
      ]
  )
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(Command("order", "make_order"))
async def cmd_order(message: Message, state: FSMContext):
  await state.clear()
  await state.set_state(OrderForm.service)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="💣 Снос Telegram аккаунта (10$)",
                  callback_data="srv_Снос_Telegram_аккаунта_10",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🔍 Доксинг (Поиск полной инфо) (20$)",
                  callback_data="srv_Доксинг_(Поиск_полной_инфо)_20",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🔓 Взлом VK (40$)", callback_data="srv_Взлом_VK_40"
              )
          ],
          [
              InlineKeyboardButton(
                  text="📸 Взлом Instagram (60$)",
                  callback_data="srv_Взлом_Instagram_60",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🎵 Взлом TikTok (80$)",
                  callback_data="srv_Взлом_TikTok_80",
              )
          ],
          [
              InlineKeyboardButton(
                  text="💬 Взлом Telegram аккаунта (100$)",
                  callback_data="srv_Взлом_Telegram_аккаунта_100",
              )
          ],
          [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")],
      ]
  )
  await message.answer(
      "<b>Выберите нужную услугу:</b>",
      reply_markup=keyboard,
      parse_mode="HTML",
  )


@router.callback_query(F.data == "reputation")
async def show_reputation(callback: CallbackQuery):
  text, keyboard = await get_reputation_text_and_keyboard()
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "contact_admin")
async def contact_admin_info(callback: CallbackQuery, state: FSMContext):
  await state.set_state(SupportForm.reason)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")]
      ]
  )
  text = (
      "💬 <b>Служба поддержки</b>\n\n"
      "<b>Шаг 1 из 2: Причина обращения</b>\n"
      "Пожалуйста, подробно объясните причину (деликатный вопрос, проблема с оплатой и т.д.):"
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.message(SupportForm.reason, F.text)
async def process_support_reason(message: Message, state: FSMContext):
  await state.update_data(reason=message.text.strip())
  await state.set_state(SupportForm.photo)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="⏩ Пропустить (Без фото)", callback_data="skip_support_photo"
              )
          ],
          [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")],
      ]
  )
  text = (
      "💬 <b>Служба поддержки</b>\n\n"
      "<b>Шаг 2 из 2: Прикрепить фото</b>\n"
      "📸 Отправьте скриншот или фотографию (например, при проблеме с оплатой), либо нажмите кнопку ниже, чтобы пропустить:"
  )
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(SupportForm.photo, F.photo)
async def process_support_photo(message: Message, state: FSMContext):
  photo_id = message.photo[-1].file_id
  data = await state.get_data()
  reason = data.get("reason")
  user = message.from_user

  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  if user.full_name:
    user_info = f"{user.full_name} ({user_info})"

  admin_text = (
      f"📩 <b>Новое обращение в поддержку!</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Причина: {reason}\n\n"
      f"💬 <i>Чтобы ответить пользователю, просто сделайте Reply (ответ) на это сообщение в боте.</i>"
  )

  if ADMIN_ID:
    try:
      await message.bot.send_photo(
          chat_id=ADMIN_ID,
          photo=photo_id,
          caption=admin_text,
          parse_mode="HTML",
      )
    except Exception as e:
      logger.error(f"Не удалось отправить обращение админу: {e}")

  success_text = (
      "✅ <b>Ваше обращение успешно отправлено!</b>\n\n"
      "Администратор рассмотрит его и ответит вам в ближайшее время."
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]
      ]
  )
  await message.answer(success_text, reply_markup=keyboard, parse_mode="HTML")
  await state.clear()


@router.callback_query(SupportForm.photo, F.data == "skip_support_photo")
async def skip_support_photo(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  reason = data.get("reason")
  user = callback.from_user

  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  if user.full_name:
    user_info = f"{user.full_name} ({user_info})"

  admin_text = (
      f"📩 <b>Новое обращение в поддержку!</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Причина: {reason}\n\n"
      f"💬 <i>Чтобы ответить пользователю, просто сделайте Reply (ответ) на это сообщение в боте.</i>"
  )

  if ADMIN_ID:
    try:
      await callback.bot.send_message(
          chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML"
      )
    except Exception as e:
      logger.error(f"Не удалось отправить обращение админу: {e}")

  success_text = (
      "✅ <b>Ваше обращение успешно отправлено!</b>\n\n"
      "Администратор рассмотрит его и ответит вам в ближайшее время."
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]
      ]
  )
  await callback.message.edit_text(
      success_text, reply_markup=keyboard, parse_mode="HTML"
  )
  await state.clear()
  await callback.answer()


@router.message(SupportForm.photo, ~F.photo)
async def process_support_photo_invalid(message: Message):
  await message.answer(
      "⚠️ Пожалуйста, отправьте именно <b>фотографию (картинкой)</b> или нажмите кнопку «Пропустить»!"
  )


# === АДМИН-ПАНЕЛЬ: ОТВЕТ ПОЛЬЗОВАТЕЛЮ ЧЕРЕЗ REPLY ===
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
    await message.reply(
        "❌ Не удалось определить ID пользователя из сообщения, на которое вы ответили."
    )
    return

  try:
    answer_text = (
        "💬 <b>Ответ от службы поддержки:</b>\n\n"
        f"{message.text or message.caption or ''}"
    )
    await message.bot.send_message(
        chat_id=target_user_id, text=answer_text, parse_mode="HTML"
    )
    await message.reply("✅ Ответ успешно доставлен пользователю!")
  except Exception as e:
    await message.reply(f"❌ Ошибка отправки сообщения пользователю: {e}")


@router.callback_query(F.data == "my_orders")
async def show_my_orders(callback: CallbackQuery):
  user_id = callback.from_user.id
  orders = USER_ORDERS.get(user_id, [])

  if not orders:
    text = (
        "📦 <b>Мои заказы</b>\n\nУ вас пока нет активных или завершенных"
        " заказов."
    )
  else:
    text = "📦 <b>Ваши заказы:</b>\n\n"
    for idx, ord_data in enumerate(orders, 1):
      text += (
          f"<b>Заказ #{idx}</b>\n• Услуга: {ord_data['service_name']}\n• Сумма:"
          f" {ord_data['total_price']}$\n• Статус: {ord_data['status']}\n\n"
      )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_home")]
      ]
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "back_home")
async def back_home(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  task_key = (callback.message.chat.id, callback.message.message_id)
  if task_key in PAYMENT_TIMERS:
    PAYMENT_TIMERS[task_key].cancel()
    del PAYMENT_TIMERS[task_key]
  ABANDONED_CHECKOUTS.pop(user_id, None)

  await state.clear()
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🛒 Сделать заказ", callback_data="start_order"
              )
          ],
          [
              InlineKeyboardButton(
                  text="📦 Мои заказы", callback_data="my_orders"
              ),
              InlineKeyboardButton(
                  text="📊 Наша репутация", callback_data="reputation"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="💬 Поддержка",
                  callback_data="contact_admin",
              )
          ],
      ]
  )
  text = (
      "<b>Добро пожаловать в сервис «КИБЕР УСЛУГИ»!</b> 💻\n\n"
      "Мы предоставляем профессиональный спектр киберуслуг и решение ваших"
      " задач быстро, качественно и конфиденциально.\n\n"
      "Выберите нужный пункт в меню ниже:"
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "start_order")
async def process_start_order(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  task_key = (callback.message.chat.id, callback.message.message_id)
  if task_key in PAYMENT_TIMERS:
    PAYMENT_TIMERS[task_key].cancel()
    del PAYMENT_TIMERS[task_key]
  ABANDONED_CHECKOUTS.pop(user_id, None)

  await state.set_state(OrderForm.service)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="💣 Снос Telegram аккаунта (10$)",
                  callback_data="srv_Снос_Telegram_аккаунта_10",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🔍 Доксинг (Поиск полной инфо) (20$)",
                  callback_data="srv_Доксинг_(Поиск_полной_инфо)_20",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🔓 Взлом VK (40$)", callback_data="srv_Взлом_VK_40"
              )
          ],
          [
              InlineKeyboardButton(
                  text="📸 Взлом Instagram (60$)",
                  callback_data="srv_Взлом_Instagram_60",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🎵 Взлом TikTok (80$)",
                  callback_data="srv_Взлом_TikTok_80",
              )
          ],
          [
              InlineKeyboardButton(
                  text="💬 Взлом Telegram аккаунта (100$)",
                  callback_data="srv_Взлом_Telegram_аккаунта_100",
              )
          ],
          [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")],
      ]
  )
  await callback.message.edit_text(
      "<b>Выберите нужную услугу:</b>",
      reply_markup=keyboard,
      parse_mode="HTML",
  )
  await callback.answer()


@router.callback_query(OrderForm.service, F.data.startswith("srv_"))
async def process_service(callback: CallbackQuery, state: FSMContext):
  parts = callback.data.split("_")
  price = int(parts[-1])
  service_name = " ".join(parts[1:-1]).replace("_", " ")

  await state.update_data(
      service_name=service_name, service_price=price, total_price=price
  )
  await state.set_state(OrderForm.target_username)

  await callback.message.edit_text(
      f"<b>Шаг 1 из 2: Юзер Telegram аккаунта цели</b>\n\n"
      f"Выбрана услуга: <b>{service_name}</b> ({price}$)\n\n"
      "Введите юзернейм Telegram аккаунта цели (например, @username):",
      parse_mode="HTML",
  )
  await callback.answer()


@router.message(OrderForm.target_username, F.text)
async def process_target_username(message: Message, state: FSMContext):
  await state.update_data(target_username=message.text.strip())
  await state.set_state(OrderForm.target_phone)
  await message.answer(
      "<b>Шаг 2 из 2: Номер телефона цели</b>\n\n"
      "Введите номер телефона цели (или укажите '-', если неизвестен):",
      parse_mode="HTML",
  )


@router.message(OrderForm.target_phone, F.text)
async def process_target_phone(message: Message, state: FSMContext):
  await state.update_data(target_phone=message.text.strip())
  data = await state.get_data()

  service_name = data.get("service_name")
  target_username = data.get("target_username")
  target_phone = data.get("target_phone")
  total_price = data.get("total_price", 0)

  summary_text = (
      "🎉 <b>Ваша заявка успешно сформирована!</b>\n\n"
      "📋 <b>Сводка заказа:</b>\n"
      f"• Услуга: <b>{service_name}</b>\n"
      f"• Юзер Telegram цели: <b>{target_username}</b>\n"
      f"• Номер телефона цели: <b>{target_phone}</b>\n\n"
      f"💵 <b>Итого к оплате:</b> <b>{total_price}$</b>\n\n"
      "⚠️ <b>Выберите способ оплаты ниже 👇</b>"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="💳 Оплатить", callback_data="go_to_payment")]
      ]
  )

  await message.answer(summary_text, reply_markup=keyboard, parse_mode="HTML")


# === ПЕРЕХОД К ВЫБОРУ ОПЛАТЫ ===
@router.callback_query(F.data == "go_to_payment")
async def process_go_to_payment(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  chat_id = callback.message.chat.id
  message_id = callback.message.message_id
  
  if user_id in ABANDONED_CHECKOUTS:
    ABANDONED_CHECKOUTS[user_id].cancel()
  ABANDONED_CHECKOUTS[user_id] = asyncio.create_task(delayed_abandoned_cart_reminder(callback.bot, user_id))

  data = await state.get_data()
  total_price = data.get("total_price", 10)

  base_text = (
      "💳 <b>ОПЛАТА ЗАКАЗА</b>\n\n"
      f"• <b>Сумма к оплате:</b> {total_price}$ USDT\n\n"
      "⚠️ Выберите удобный способ оплаты ниже 👇\n"
  )

  def get_payment_keyboard(timer_active=False, time_left="20:00"):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🤖 Cryptobot",
                    callback_data="pay_cryptobot",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🪙 Оплата криптой (реквизиты)",
                    callback_data="crypto_expert",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📖 Как купить крипту (Минигайд)",
                    callback_data="cryptobot_guide",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Не знаю как оплатить / Связаться",
                    callback_data="payment_help_manager",
                )
            ]
        ]
    )

  initial_text = f"⏳ <b>Успейте оплатить! До окончания бронирования осталось: <u>20:00</u></b>\n\n{base_text}"
  await callback.message.edit_text(initial_text, reply_markup=get_payment_keyboard(), parse_mode="HTML")
  
  task_key = (chat_id, message_id)
  if task_key in PAYMENT_TIMERS:
    PAYMENT_TIMERS[task_key].cancel()
  
  PAYMENT_TIMERS[task_key] = asyncio.create_task(
      run_payment_timer(callback.bot, chat_id, message_id, base_text, get_payment_keyboard)
  )
  
  await callback.answer()


@router.callback_query(F.data == "payment_help_manager")
async def process_payment_help_manager(callback: CallbackQuery):
  user = callback.from_user
  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  if user.full_name:
    user_info = f"{user.full_name} ({user_info})"

  admin_text = (
      f"🚨 <b>Проблема с оплатой / Запрос помощи!</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Ситуация: Клиент на этапе выбора оплаты нажал кнопку «Не знаю как оплатить / Связаться» (нужна помощь с переводом)."
  )

  if ADMIN_ID:
    try:
      await callback.bot.send_message(
          chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML"
      )
    except Exception as e:
      logger.error(f"Не удалось отправить уведомление о проблеме с оплатой админу: {e}")

  await callback.answer(
      "✅ Менеджер уведомлен! В ближайшее время он свяжется с вами и поможет.",
      show_alert=True,
  )


# === МИНИГАЙД ПО ПОКУПКЕ КРИПТЫ ЧЕРЕЗ P2P ===
@router.callback_query(F.data == "cryptobot_guide")
async def process_cryptobot_guide(callback: CallbackQuery):
  chat_id = callback.message.chat.id
  message_id = callback.message.message_id
  task_key = (chat_id, message_id)
  if task_key in PAYMENT_TIMERS:
    PAYMENT_TIMERS[task_key].cancel()
    del PAYMENT_TIMERS[task_key]

  guide_text = (
      "📖 <b>Минигайд: Как легко оплатить через CryptoBot</b>\n\n"
      "Если у вас нет криптовалюты, вы можете купить её за пару минут прямо с банковской карты (СБП, RUB, BYN, KZT и др.) через P2P:\n\n"
      "1. Перейдите в официальный бот <b>@CryptoBot</b> (или нажмите кнопку «Cryptobot» при оплате).\n"
      "2. В боте перейдите в раздел <b>«Кошелек» -> «P2P»</b> (или Маркет).\n"
      "3. Выберите покупку USDT за рубли, тенге, белорусские рубли или другую валюту с помощью вашей банковской карты (СБП / карта).\n"
      "4. После покупки USDT появятся на вашем балансе в CryptoBot.\n"
      "5. Вернитесь в наш бот, нажмите кнопку <b>«Cryptobot»</b> и подтвердите оплату — средства спишутся мгновенно и без комиссии!\n\n"
      "Это максимально быстро, безопасно и удобно."
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="⬅️ Назад к оплате", callback_data="go_to_payment")]
      ]
  )
  await callback.message.edit_text(guide_text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "noop")
async def process_noop(callback: CallbackQuery):
  await callback.answer("", show_alert=False)


# === ОБРАБОТКА ОПЛАТЫ ЧЕРЕЗ CRYPTOBOT ===
@router.callback_query(F.data == "pay_cryptobot")
async def process_pay_cryptobot(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  chat_id = callback.message.chat.id
  message_id = callback.message.message_id

  task_key = (chat_id, message_id)
  if task_key in PAYMENT_TIMERS:
    PAYMENT_TIMERS[task_key].cancel()
    del PAYMENT_TIMERS[task_key]
  ABANDONED_CHECKOUTS.pop(user_id, None)

  data = await state.get_data()
  total_price = data.get("total_price", 10)

  pay_url = await create_cryptobot_invoice(
      total_price, "Оплата киберуслуг"
  )
  if not pay_url:
    await callback.answer(
        "❌ Ошибка: Криптобот не настроен администратором на сервере!",
        show_alert=True,
    )
    return

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🔗 Оплатить счет без комиссии", url=pay_url)],
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), проверить",
                  callback_data="cryptobot_check_success",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад к выбору", callback_data="go_to_payment"
              )
          ],
      ]
  )
  text = (
      "🤖 <b>Оплата через CryptoBot</b>\n\n"
      f"Сумма: <b>{total_price} USDT</b> (Без комиссий)\n\n"
      "Нажмите кнопку ниже для перехода на официальный защищенный платежный шлюз CryptoBot. После оплаты нажмите кнопку «Я оплатил(-а), проверить»."
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "cryptobot_check_success")
async def process_cryptobot_check(callback: CallbackQuery, state: FSMContext):
  user = callback.from_user
  data = await state.get_data()

  order_info = {
      "service_name": data.get("service_name"),
      "total_price": data.get("total_price"),
      "status": "⏳ Ожидает проверки платежа CryptoBot",
  }
  if user.id not in USER_ORDERS:
    USER_ORDERS[user.id] = []
  USER_ORDERS[user.id].append(order_info)

  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  if user.full_name:
    user_info = f"{user.full_name} ({user_info})"

  admin_order_text = (
      f"🤖 <b>Заказ с оплатой через CryptoBot!</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Услуга: <b>{data.get('service_name')}</b>\n"
      f"• Юзер цели: {data.get('target_username')}\n"
      f"• Телефон цели: {data.get('target_phone')}\n"
      f"• Сумма: <b>{data.get('total_price')}$</b>\n\n"
      f"💬 <i>Оплачено через CryptoBot. Проверьте поступление в боте. Сделайте Reply на это сообщение для ответа.</i>"
  )
  if ADMIN_ID:
    try:
      await callback.bot.send_message(
          chat_id=ADMIN_ID, text=admin_order_text, parse_mode="HTML"
      )
    except Exception as e:
      logger.error(f"Не удалось отправить уведомление админу: {e}")

  final_text = (
      "✅ <b>Платеж обрабатывается!</b>\n\n"
      "Заявка принята. Модератор проверит поступление средств через CryptoBot и свяжется с вами в течение <b>25 минут</b>."
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]
      ]
  )
  await callback.message.edit_text(final_text, reply_markup=keyboard, parse_mode="HTML")
  await state.clear()


# === ВЫБОР СПОСОБА ОПЛАТЫ (BEP-20 / TON / TRC-20 / BTC) ===
@router.callback_query(F.data == "crypto_expert")
async def process_crypto_expert(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  chat_id = callback.message.chat.id
  message_id = callback.message.message_id

  task_key = (chat_id, message_id)
  if task_key in PAYMENT_TIMERS:
    PAYMENT_TIMERS[task_key].cancel()
    del PAYMENT_TIMERS[task_key]
  ABANDONED_CHECKOUTS.pop(user_id, None)

  text = (
      "💳 <b>ВЫБОР СЕТИ USDT / КРИПТОВАЛЮТЫ</b>\n\n"
      "Пожалуйста, выберите криптовалюту или сеть для перевода:"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🔴 USDT (TRC-20)", callback_data="pay_trc20"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🟢 USDT (BEP-20)", callback_data="pay_bep20"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🔵 TON", callback_data="pay_ton"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🟠 Bitcoin (BTC)", callback_data="pay_btc"
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="go_to_payment"
              )
          ],
      ]
  )

  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


# === ОПЛАТА TRC-20 ===
@router.callback_query(F.data == "pay_trc20")
async def process_pay_trc20(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  total_price = data.get("total_price", 10)

  payment_text = (
      "💳 <b>ОПЛАТА USDT (TRC-20)</b>\n\n"
      f"• <b>Сумма:</b> {total_price}$\n"
      "• <b>Сеть:</b> TRC-20\n"
      "• <b>Реквизиты для оплаты:</b>\n"
      "<code>TYd1ekJgTHnFfgAVYXsi7689GaG5oZN3Xe</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "<i>Переводите строго точную сумму в сети TRC-20. После оплаты обязательно"
      " нажмите кнопку ниже и отправьте чек! Без чека заявка не будет"
      " обработана.</i>"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), прикрепить чек",
                  callback_data="ask_for_receipt",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад к выбору способа оплаты",
                  callback_data="crypto_expert",
              )
          ],
      ]
  )

  await callback.message.edit_text(
      payment_text, reply_markup=keyboard, parse_mode="HTML"
  )
  await callback.answer()


# === ОПЛАТА BEP-20 ===
@router.callback_query(F.data == "pay_bep20")
async def process_pay_bep20(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  total_price = data.get("total_price", 10)

  payment_text = (
      "💳 <b>ОПЛАТА USDT (BEP-20)</b>\n\n"
      f"• <b>Сумма:</b> {total_price}$\n"
      "• <b>Сеть:</b> BEP-20\n"
      "• <b>Реквизиты для оплаты:</b>\n"
      "<code>0x0A6C6634f8FD920a5E9E0d241CD46d6C15377f39</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "<i>Переводите строго точную сумму в сети BEP-20. После оплаты обязательно"
      " нажмите кнопку ниже и отправьте чек! Без чека заявка не будет"
      " обработана.</i>"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), прикрепить чек",
                  callback_data="ask_for_receipt",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад к выбору способа оплаты",
                  callback_data="crypto_expert",
              )
          ],
      ]
  )

  await callback.message.edit_text(
      payment_text, reply_markup=keyboard, parse_mode="HTML"
  )
  await callback.answer()


# === ОПЛАТА TON ===
@router.callback_query(F.data == "pay_ton")
async def process_pay_ton(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  total_price = data.get("total_price", 10)

  payment_text = (
      "💳 <b>ОПЛАТА TON</b>\n\n"
      f"• <b>Сумма:</b> {total_price}$\n"
      "• <b>Сеть:</b> TON\n"
      "• <b>Реквизиты для оплаты:</b>\n"
      "<code>UQDwk7PZoWr0zHv-BItBEGxnq4uyj81z6BlIw74heDgF-Shd</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "<i>Переводите строго точную сумму в сети TON. После оплаты обязательно"
      " нажмите кнопку ниже и отправьте чек! Без чека заявка не будет"
      " обработана.</i>"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), прикрепить чек",
                  callback_data="ask_for_receipt",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад к выбору способа оплаты",
                  callback_data="crypto_expert",
              )
          ],
      ]
  )

  await callback.message.edit_text(
      payment_text, reply_markup=keyboard, parse_mode="HTML"
  )
  await callback.answer()


# === ОПЛАТА BITCOIN (BTC) ===
@router.callback_query(F.data == "pay_btc")
async def process_pay_btc(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  total_price = data.get("total_price", 10)

  payment_text = (
      "💳 <b>ОПЛАТА BITCOIN (BTC)</b>\n\n"
      f"• <b>Сумма:</b> {total_price}$\n"
      "• <b>Сеть:</b> Bitcoin\n"
      "• <b>Реквизиты для оплаты:</b>\n"
      "<code>bc1qj7xy0645vcza7l3et5s8nuahllua9a439c0795</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "<i>Переводите строго точную сумму в сети Bitcoin. После оплаты обязательно"
      " нажмите кнопку ниже и отправьте чек! Без чека заявка не будет"
      " обработана.</i>"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), прикрепить чек",
                  callback_data="ask_for_receipt",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад к выбору способа оплаты",
                  callback_data="crypto_expert",
              )
          ],
      ]
  )

  await callback.message.edit_text(
      payment_text, reply_markup=keyboard, parse_mode="HTML"
  )
  await callback.answer()


# === ЗАПРОС ЧЕКА У ПОЛЬЗОВАТЕЛЯ ===
@router.callback_query(F.data == "ask_for_receipt")
async def ask_for_receipt(callback: CallbackQuery, state: FSMContext):
  await state.set_state(OrderForm.receipt)
  text = (
      "🧾 <b>Подтверждение оплаты</b>\n\n"
      "Пожалуйста, отправьте в чат <b>фотографию или скриншот чека</b> о"
      " переводе (без чека система не пропустит вашу заявку)."
  )
  await callback.message.edit_text(text, parse_mode="HTML")
  await callback.answer()


# === ПОЛУЧЕНИЕ ЧЕКА И СОХРАНЕНИЕ ===
@router.message(OrderForm.receipt, F.photo)
async def process_receipt_photo(message: Message, state: FSMContext):
  user = message.from_user
  user_id = user.id
  ABANDONED_CHECKOUTS.pop(user_id, None)

  data = await state.get_data()

  order_info = {
      "service_name": data.get("service_name"),
      "total_price": data.get("total_price"),
      "status": "⏳ Ожидает проверки оплаты",
  }

  if user_id not in USER_ORDERS:
    USER_ORDERS[user_id] = []
  USER_ORDERS[user_id].append(order_info)

  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  if user.full_name:
    user_info = f"{user.full_name} ({user_info})"

  admin_order_text = (
      f"🛒 <b>Новый оплаченный заказ!</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Услуга: <b>{data.get('service_name')}</b>\n"
      f"• Юзер цели: {data.get('target_username')}\n"
      f"• Телефон цели: {data.get('target_phone')}\n"
      f"• Сумма: <b>{data.get('total_price')}$</b>\n\n"
      f"💬 <i>Чек прикреплен выше. Сделайте Reply на этом сообщении, чтобы ответить пользователю.</i>"
  )
  if ADMIN_ID:
    try:
      photo_id = message.photo[-1].file_id
      await message.bot.send_photo(
          chat_id=ADMIN_ID,
          photo=photo_id,
          caption=admin_order_text,
          parse_mode="HTML",
      )
    except Exception as e:
      logger.error(f"Не удалось отправить заказ админу: {e}")

  final_text = (
      "✅ <b>Заявка и чек успешно приняты!</b>\n\n"
      "Администратор проверяет поступление средств. С вами свяжутся в"
      " течение <b>25 минут</b>."
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🏠 На главную", callback_data="back_home"
              )
          ]
      ]
  )

  await message.answer(final_text, reply_markup=keyboard, parse_mode="HTML")
  await state.clear()


@router.message(OrderForm.receipt, ~F.photo)
async def process_receipt_invalid(message: Message):
  await message.answer(
      "⚠️ Пожалуйста, отправьте именно <b>фотографию или скриншот чека</b>"
      " (картинкой), текстовые сообщения в качестве чека не принимаются!"
  )


# === ВЕБ-СЕРВЕР ДЛЯ ЗАЩИТЫ ОТ СНА СЕРВЕРА (RENDER) ===
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
  bot = Bot(token=BOT_TOKEN)
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
