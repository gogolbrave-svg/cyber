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
    "Россия": {"code": "RUB", "symbol": "₽"},
    "Беларусь": {"code": "BYN", "symbol": "BYN"},
    "Казахстан": {"code": "KZT", "symbol": "₸"},
    "Молдова": {"code": "MDL", "symbol": "лей"},
    "Узбекистан": {"code": "UZS", "symbol": "сум"},
    "Кыргызстан": {"code": "KGS", "symbol": "сом"},
    "Таджикистан": {"code": "TJS", "symbol": "сом."},
    "Грузия": {"code": "GEL", "symbol": "lari"},
    "Армения": {"code": "AMD", "symbol": "драм"},
    "Азербайджан": {"code": "AZN", "symbol": "AZN"},
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
      "✨ <b>Специальное предложение</b>\n\n"
      f"Напоминаем, что в нашем сервисе действует персональная скидка <b>20%</b>, которая актуальна до <b>{target_day} {month_name}</b>.\n\n"
      "Будем рады помочь с решением вашей задачи на лучших условиях. Гарантируем аккуратность и конфиденциальность.\n\n"
      "Ждем вас в боте 👇"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✨ Оформить со скидкой 20%",
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
          "👋 <b>Завершить оформление?</b>\n\n"
          "Мы заметили, что вы остановились на шаге оплаты. Если у вас возникли вопросы по переводу или деталей заказа — наша поддержка всегда на связи.\n\n"
          "Вернуться к оформлению можно в один клик 👇"
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
          f"⏳ <b>Бронирование зафиксировано</b>\n"
          f"Время на завершение оплаты: <b><u>{timer_str}</u></b>\n\n"
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
        "⌛ <b>Время бронирования истекло</b>\n\n"
        "Срок резерва по заказу завершился. Вы можете повторить оформление в пару кликов."
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
      "📊 <b>Статистика и достижения сервиса</b>\n\n"
      f"• Успешно выполнено задач: <b>{completed:,}</b>\n"
      f"• В активной работе: <b>{in_progress}</b>\n"
      f"• Новых заявок сегодня: <b>{new_requests}</b>\n\n"
      "<i>Мы дорожим конфиденциальностью каждого клиента и поддерживаем безупречный уровень сервиса.</i>"
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
                  text="📊 Репутация", callback_data="reputation"
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
      "<b>Добро пожаловать в «КИБЕР УСЛУГИ»</b> 💻\n\n"
      "Профессиональное решение ваших цифровых задач: оперативно, надежно и в полной конфиденциальности.\n\n"
      "Выберите интересующий раздел в меню ниже:"
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
        "📦 <b>Мои заказы</b>\n\n"
        "У вас пока нет оформленных заказов. Вы можете создать первый в главном меню."
    )
  else:
    text = "📦 <b>История ваших заказов:</b>\n\n"
    for idx, ord_data in enumerate(orders, 1):
      text += (
          f"<b>Заказ #{idx}</b>\n"
          f"• Услуга: {ord_data['service_name']}\n"
          f"• Сумма: {ord_data['total_price']}$\n"
          f"• Статус: {ord_data['status']}\n\n"
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
                  text="💣 Снос Telegram аккаунта — 10$",
                  callback_data="srv_Снос_Telegram_аккаунта_10",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🔍 Доксинг (поиск информации) — 20$",
                  callback_data="srv_Доксинг_(Поиск_полной_инфо)_20",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🔓 Взлом VK — 40$", callback_data="srv_Взлом_VK_40"
              )
          ],
          [
              InlineKeyboardButton(
                  text="📸 Взлом Instagram — 60$",
                  callback_data="srv_Взлом_Instagram_60",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🎵 Взлом TikTok — 80$",
                  callback_data="srv_Взлом_TikTok_80",
              )
          ],
          [
              InlineKeyboardButton(
                  text="💬 Взлом Telegram аккаунта — 100$",
                  callback_data="srv_Взлом_Telegram_аккаунта_100",
              )
          ],
          [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")],
      ]
  )
  await message.answer(
      "<b>Выбор услуги</b>\n\nВыберите подходящий вариант из списка ниже:",
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
      "<b>Шаг 1 из 2: Суть вопроса</b>\n\n"
      "Опишите вашу ситуацию или задайте вопрос (деликатная деталь, статус платежа и т.д.):"
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
                  text="⏩ Пропустить (без фото)", callback_data="skip_support_photo"
              )
          ],
          [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")],
      ]
  )
  text = (
      "💬 <b>Служба поддержки</b>\n\n"
      "<b>Шаг 2 из 2: Дополнительное фото</b>\n\n"
      "📸 Если у вас есть скриншот или фото для примера, отправьте его в чат, либо нажмите кнопку пропуска:"
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
      f"📩 <b>Новое обращение в поддержку</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Вопрос: {reason}\n\n"
      f"💬 <i>Для ответа просто сделайте Reply (ответ) на это сообщение.</i>"
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
      "✅ <b>Обращение отправлено</b>\n\n"
      "Специалист поддержки изучит ваш вопрос и свяжется с вами в ближайшее время."
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
      f"📩 <b>Новое обращение в поддержку</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Вопрос: {reason}\n\n"
      f"💬 <i>Для ответа просто сделайте Reply (ответ) на это сообщение.</i>"
  )

  if ADMIN_ID:
    try:
      await callback.bot.send_message(
          chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML"
      )
    except Exception as e:
      logger.error(f"Не удалось отправить обращение админу: {e}")

  success_text = (
      "✅ <b>Обращение отправлено</b>\n\n"
      "Специалист поддержки изучит ваш вопрос и свяжется с вами в ближайшее время."
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
      "📎 Пожалуйста, отправьте <b>изображение (картинкой)</b> либо нажмите кнопку «Пропустить»."
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
        "❌ Не удалось распознать ID пользователя из выбранного сообщения."
    )
    return

  try:
    answer_text = (
        "💬 <b>Ответ от поддержки:</b>\n\n"
        f"{message.text or message.caption or ''}"
    )
    await message.bot.send_message(
        chat_id=target_user_id, text=answer_text, parse_mode="HTML"
    )
    await message.reply("✅ Ответ успешно отправлен пользователю.")
  except Exception as e:
    await message.reply(f"❌ Не удалось отправить сообщение: {e}")


@router.callback_query(F.data == "my_orders")
async def show_my_orders(callback: CallbackQuery):
  user_id = callback.from_user.id
  orders = USER_ORDERS.get(user_id, [])

  if not orders:
    text = (
        "📦 <b>Мои заказы</b>\n\n"
        "У вас пока нет активных или завершенных заказов."
    )
  else:
    text = "📦 <b>История ваших заказов:</b>\n\n"
    for idx, ord_data in enumerate(orders, 1):
      text += (
          f"<b>Заказ #{idx}</b>\n"
          f"• Услуга: {ord_data['service_name']}\n"
          f"• Сумма: {ord_data['total_price']}$\n"
          f"• Статус: {ord_data['status']}\n\n"
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
                  text="📊 Репутация", callback_data="reputation"
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
      "<b>Добро пожаловать в «КИБЕР УСЛУГИ»</b> 💻\n\n"
      "Профессиональное решение ваших цифровых задач: оперативно, надежно и в полной конфиденциальности.\n\n"
      "Выберите интересующий раздел в меню ниже:"
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
                  text="💣 Снос Telegram аккаунта — 10$",
                  callback_data="srv_Снос_Telegram_аккаунта_10",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🔍 Доксинг (поиск информации) — 20$",
                  callback_data="srv_Доксинг_(Поиск_полной_инфо)_20",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🔓 Взлом VK — 40$", callback_data="srv_Взлом_VK_40"
              )
          ],
          [
              InlineKeyboardButton(
                  text="📸 Взлом Instagram — 60$",
                  callback_data="srv_Взлом_Instagram_60",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🎵 Взлом TikTok — 80$",
                  callback_data="srv_Взлом_TikTok_80",
              )
          ],
          [
              InlineKeyboardButton(
                  text="💬 Взлом Telegram аккаунта — 100$",
                  callback_data="srv_Взлом_Telegram_аккаунта_100",
              )
          ],
          [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")],
      ]
  )
  await callback.message.edit_text(
      "<b>Выбор услуги</b>\n\nВыберите подходящий вариант из списка ниже:",
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
      f"Выбрано: <b>{service_name}</b> ({price}$)\n\n"
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
      "✨ <b>Анкета успешно сформирована</b>\n\n"
      "📋 <b>Детали вашего заказа:</b>\n"
      f"• Услуга: <b>{service_name}</b>\n"
      f"• Юзер Telegram цели: <b>{target_username}</b>\n"
      f"• Номер телефона цели: <b>{target_phone}</b>\n\n"
      f"💵 <b>К оплате:</b> <b>{total_price}$</b>\n\n"
      "💳 Перейдите к оплате удобным способом с помощью кнопки ниже 👇"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="💳 Перейти к оплате", callback_data="go_to_payment")]
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
      "💳 <b>ВЫБОР СПОСОБА ОПЛАТЫ</b>\n\n"
      f"• Сумма к зачислению: <b>{total_price}$ USDT</b>\n\n"
      "Выберите наиболее удобный вариант из списка ниже:\n"
  )

  def get_payment_keyboard(timer_active=False, time_left="20:00"):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🤖 CryptoBot (мгновенно и без комиссий)",
                    callback_data="pay_cryptobot",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🪙 Криптовалюта (выбор сети / реквизиты)",
                    callback_data="crypto_expert",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📖 Как приобрести криптовалюту (Минигайд)",
                    callback_data="cryptobot_guide",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Нужна помощь с оплатой",
                    callback_data="payment_help_manager",
                )
            ]
        ]
    )

  initial_text = f"⏳ <b>Бронирование зафиксировано</b>\nВремя на завершение оплаты: <b><u>20:00</u></b>\n\n{base_text}"
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
      f"🚨 <b>Запрос помощи с оплатой</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Ситуация: Клиенту требуется консультация по проведению платежа."
  )

  if ADMIN_ID:
    try:
      await callback.bot.send_message(
          chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML"
      )
    except Exception as e:
      logger.error(f"Не удалось отправить уведомление о проблеме с оплатой админу: {e}")

  await callback.answer(
      "✅ Менеджер уведомлен. Скоро он свяжется с вами для помощи.",
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
      "📖 <b>Минигайд: как оплатить через CryptoBot</b>\n\n"
      "Если у вас нет готовой криптовалюты, вы можете легко приобрести её с банковской карты (СБП, RUB, BYN, KZT и др.) за пару минут:\n\n"
      "1. Откройте официальный бот <b>@CryptoBot</b>.\n"
      "2. Перейдите в раздел <b>«Кошелек» ➔ «P2P»</b>.\n"
      "3. Выберите покупку USDT за вашу привычную валюту с помощью карты.\n"
      "4. После завершения сделки USDT поступят на ваш баланс в боте.\n"
      "5. Вернитесь сюда, нажмите <b>CryptoBot</b> и подтвердите платеж без комиссий.\n\n"
      "Быстро, безопасно и полностью конфиденциально."
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
        "❌ Сервис временно недоступен. Обратитесь в поддержку.",
        show_alert=True,
    )
    return

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🔗 Оплатить счет (без комиссии)", url=pay_url)],
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), проверить",
                  callback_data="cryptobot_check_success",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="go_to_payment"
              )
          ],
      ]
  )
  text = (
      "🤖 <b>Оплата через CryptoBot</b>\n\n"
      f"Сумма к оплате: <b>{total_price} USDT</b>\n\n"
      "Нажмите кнопку перевода для совершения платежа через защищенный шлюз, после чего нажмите кнопку проверки ниже."
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
      f"🤖 <b>Новый платеж через CryptoBot</b>\n\n"
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
      "✅ <b>Платеж принят в обработку</b>\n\n"
      "Система проверяет зачисление средств. Модератор подтвердит заявку в течение <b>25 минут</b>."
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
      "🪙 <b>ВЫБОР СЕТИ / КРИПТОВАЛЮТЫ</b>\n\n"
      "Выберите подходящую сеть для перевода средств:"
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
      f"• Сумма: <b>{total_price}$</b>\n"
      "• Сеть: <b>TRC-20</b>\n"
      "• Реквизиты:\n"
      "<code>TYd1ekJgTHnFfgAVYXsi7689GaG5oZN3Xe</code>\n\n"
      "💡 <i>Переведите точную сумму. После завершения перевода нажмите кнопку отправки чека.</i>"
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
                  text="⬅️ Назад",
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
      f"• Сумма: <b>{total_price}$</b>\n"
      "• Сеть: <b>BEP-20</b>\n"
      "• Реквизиты:\n"
      "<code>0x0A6C6634f8FD920a5E9E0d241CD46d6C15377f39</code>\n\n"
      "💡 <i>Переведите точную сумму. После завершения перевода нажмите кнопку отправки чека.</i>"
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
                  text="⬅️ Назад",
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
      f"• Сумма: <b>{total_price}$</b>\n"
      "• Сеть: <b>TON</b>\n"
      "• Реквизиты:\n"
      "<code>UQDwk7PZoWr0zHv-BItBEGxnq4uyj81z6BlIw74heDgF-Shd</code>\n\n"
      "💡 <i>Переведите точную сумму. После завершения перевода нажмите кнопку отправки чека.</i>"
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
                  text="⬅️ Назад",
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
      f"• Сумма: <b>{total_price}$</b>\n"
      "• Сеть: <b>Bitcoin</b>\n"
      "• Реквизиты:\n"
      "<code>bc1qj7xy0645vcza7l3et5s8nuahllua9a439c0795</code>\n\n"
      "💡 <i>Переведите точную сумму. После завершения перевода нажмите кнопку отправки чека.</i>"
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
                  text="⬅️ Назад",
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
      "🧾 <b>Подтверждение платежа</b>\n\n"
      "Отправьте в чат <b>скриншот или фотографию чека</b> о выполненном переводе для проверки."
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
      f"🛒 <b>Новая заявка с чеком</b>\n\n"
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
      "✅ <b>Заявка и чек успешно получены</b>\n\n"
      "Администратор проверяет платеж. Мы свяжемся с вами в течение <b>25 минут</b>."
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
      "📎 Пожалуйста, прикрепите <b>скриншот или фото чека изображением</b>. Текстовые сообщения не принимаются."
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
