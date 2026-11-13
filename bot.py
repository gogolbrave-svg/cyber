import asyncio
import datetime
import logging
import os
import sqlite3
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

# === ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ SQLITE ===
def init_db():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            service_name TEXT,
            total_price REAL,
            status TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def add_order_db(user_id: int, service_name: str, total_price: float, status: str) -> int:
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    created_at = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    cursor.execute(
        "INSERT INTO orders (user_id, service_name, total_price, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, service_name, total_price, status, created_at)
    )
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return order_id

def get_user_orders_db(user_id: int):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, service_name, total_price, status, created_at FROM orders WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


class OrderForm(StatesGroup):
    service = State()
    target_username = State()
    target_phone = State()
    receipt = State()

class SupportForm(StatesGroup):
    reason = State()


router = Router()


# === ЕДИНЫЕ КЛАВИАТУРЫ И ФУНКЦИИ ===
def get_main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💻 Каталог услуг", callback_data="catalog"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📦 Мои заявки", callback_data="my_orders"
                ),
                InlineKeyboardButton(
                    text="📊 О сервисе", callback_data="about_service"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💬 Поддержка", callback_data="support"
                )
            ],
        ]
    )

def get_back_home_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]
        ]
    )

def get_cancel_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")]
        ]
    )

def get_order_details_text(data):
    service_name = data.get("service_name")
    target_username = data.get("target_username")
    target_phone = data.get("target_phone")

    if service_name == "Взлом VK":
        return f"• Номер или ссылка на VK: <b>{target_username}</b>\n"
    elif service_name == "Взлом Instagram":
        return f"• Юзер Instagram или ссылка: <b>{target_username}</b>\n"
    elif service_name == "Взлом TikTok":
        return f"• Юзер TikTok или ссылка: <b>{target_username}</b>\n"
    else:
        return (
            f"• Юзер Telegram цели: <b>{target_username}</b>\n"
            f"• Номер телефона цели: <b>{target_phone}</b>\n"
        )

def get_admin_order_details_text(data):
    service_name = data.get("service_name")
    target_username = data.get("target_username")
    target_phone = data.get("target_phone")

    if service_name == "Взлом VK":
        return f"• Цель (Номер / ссылка): {target_username}\n"
    elif service_name == "Взлом Instagram":
        return f"• Цель (Юзер / ссылка): {target_username}\n"
    elif service_name == "Взлом TikTok":
        return f"• Цель (Юзер / ссылка): {target_username}\n"
    else:
        return (
            f"• Юзер цели: {target_username}\n"
            f"• Телефон цели: {target_phone}\n"
        )


# === 0. ГЛАВНОЕ МЕНЮ И КОМАНДЫ ===
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    text = (
        "<b>КИБЕР УСЛУГИ</b>\n"
        "ЦИФРОВЫЕ РЕШЕНИЯ И ПОДДЕРЖКА\n\n"
        "Добро пожаловать!\n\n"
        "Здесь вы можете выбрать интересующую услугу из каталога ниже:"
    )
    await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="HTML")


@router.callback_query(F.data == "back_home")
async def back_home_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "<b>КИБЕР УСЛУГИ</b>\n"
        "ЦИФРОВЫЕ РЕШЕНИЯ И ПОДДЕРЖКА\n\n"
        "Добро пожаловать!\n\n"
        "Здесь вы можете выбрать интересующую услугу из каталога ниже:"
    )
    await callback.message.edit_text(text, reply_markup=get_main_keyboard(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "about_service")
async def show_about_service(callback: CallbackQuery):
    text = (
        "📊 <b>О сервисе</b>\n\n"
        "• Успешно выполнено задач: <b>3,752+</b>\n"
        "• В активной работе: <b>240</b>\n"
        "• Новых заявок сегодня: <b>45</b>\n\n"
        "<i>Мы дорожим конфиденциальностью каждого клиента и поддерживаем безупречный уровень сервиса.</i>"
    )
    await callback.message.edit_text(text, reply_markup=get_back_home_keyboard(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "my_orders")
async def show_my_orders(callback: CallbackQuery):
    user_id = callback.from_user.id
    orders = get_user_orders_db(user_id)

    if not orders:
        text = (
            "📦 <b>Мои заявки</b>\n\n"
            "У вас пока нет оформленных заявок."
        )
    else:
        text = "📦 <b>Мои заявки:</b>\n\n"
        for ord_id, s_name, price, status, created in orders:
            text += (
                f"<b>Заявка №{ord_id}</b>   <code>{status}</code>\n"
                f"Создана: {created}\n"
                f"• Услуга: <b>{s_name}</b>\n"
                f"• Сумма: <b>{price}$</b>\n\n"
                f"<i>По вопросам заявки можно обратиться в поддержку.</i>\n"
                f"-----------------------------------\n"
            )

    await callback.message.edit_text(text, reply_markup=get_back_home_keyboard(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "catalog")
async def show_catalog(callback: CallbackQuery, state: FSMContext):
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
            [InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")],
        ]
    )
    text = "💻 <b>Каталог услуг</b>\n\nВыберите подходящий вариант из списка ниже:"
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(OrderForm.service, F.data.startswith("srv_"))
async def process_service(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    price = float(parts[-1])
    service_name = " ".join(parts[1:-1]).replace("_", " ")

    await state.update_data(
        service_name=service_name, service_price=price, total_price=price
    )
    await state.set_state(OrderForm.target_username)

    if service_name == "Взлом VK":
        prompt_text = (
            f"<b>Номер или ссылка на VK</b>\n\n"
            f"Выбрано: <b>{service_name}</b> ({price}$)\n\n"
            "Введите номер или ссылку на VK:"
        )
    elif service_name == "Взлом Instagram":
        prompt_text = (
            f"<b>Юзер Instagram или ссылка</b>\n\n"
            f"Выбрано: <b>{service_name}</b> ({price}$)\n\n"
            "Введите юзер инстаграм или ссылку:"
        )
    elif service_name == "Взлом TikTok":
        prompt_text = (
            f"<b>Юзер TikTok или ссылка</b>\n\n"
            f"Выбрано: <b>{service_name}</b> ({price}$)\n\n"
            "Введите юзер тик ток или ссылку:"
        )
    else:
        prompt_text = (
            f"<b>Юзер Telegram аккаунта цели</b>\n\n"
            f"Выбрано: <b>{service_name}</b> ({price}$)\n\n"
            "Введите юзернейм Telegram аккаунта цели (например, @username):"
        )

    await callback.message.edit_text(prompt_text, reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    await callback.answer()


@router.message(OrderForm.target_username, F.text)
async def process_target_username(message: Message, state: FSMContext):
    target_val = message.text.strip()
    await state.update_data(target_username=target_val)
    data = await state.get_data()
    service_name = data.get("service_name")

    if service_name in ["Взлом VK", "Взлом Instagram", "Взлом TikTok"]:
        await state.update_data(target_phone="-")
        data = await state.get_data()
        total_price = data.get("total_price", 0)

        summary_text = (
            "✅ <b>Заявка готова</b>\n\n"
            "📋 <b>Детали вашей заявки:</b>\n"
            f"• Услуга: <b>{service_name}</b>\n"
            f"{get_order_details_text(data)}"
            f"💵 <b>К оплате:</b> <b>{total_price}$</b>\n\n"
            "💳 Перейдите к оплате удобным способом с помощью кнопки ниже 👇"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 Способ оплаты", callback_data="go_to_payment")],
                [InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]
            ]
        )
        await message.answer(summary_text, reply_markup=keyboard, parse_mode="HTML")
    else:
        await state.set_state(OrderForm.target_phone)
        await message.answer(
            "<b>Номер телефона цели</b>\n\n"
            "Введите номер телефона цели (или укажите '-', если неизвестен):",
            reply_markup=get_cancel_keyboard(),
            parse_mode="HTML",
        )


@router.message(OrderForm.target_phone, F.text)
async def process_target_phone(message: Message, state: FSMContext):
    await state.update_data(target_phone=message.text.strip())
    data = await state.get_data()

    service_name = data.get("service_name")
    total_price = data.get("total_price", 0)

    summary_text = (
        "✅ <b>Заявка готова</b>\n\n"
        "📋 <b>Детали вашей заявки:</b>\n"
        f"• Услуга: <b>{service_name}</b>\n"
        f"{get_order_details_text(data)}"
        f"💵 <b>К оплате:</b> <b>{total_price}$</b>\n\n"
        "💳 Перейдите к оплате удобным способом с помощью кнопки ниже 👇"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Способ оплаты", callback_data="go_to_payment")],
            [InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]
        ]
    )
    await message.answer(summary_text, reply_markup=keyboard, parse_mode="HTML")


# === ПОДДЕРЖКА ===
@router.callback_query(F.data == "support")
async def support_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SupportForm.reason)
    text = (
        "💬 <b>Поддержка</b>\n\n"
        "Опишите, с чем нужна помощь. Вы также можете прикрепить фото или скриншот к вашему сообщению."
    )
    await callback.message.edit_text(text, reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    await callback.answer()


@router.message(SupportForm.reason)
async def process_support_message(message: Message, state: FSMContext):
    user = message.from_user
    user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
    if user.full_name:
        user_info = f"{user.full_name} ({user_info})"

    reason_text = message.text or message.caption or "Без текста"
    photo_id = message.photo[-1].file_id if message.photo else None

    admin_text = (
        f"📩 <b>Новое обращение в поддержку</b>\n\n"
        f"• От кого: {user_info}\n"
        f"• User ID: <code>{user.id}</code>\n"
        f"• Вопрос: {reason_text}\n\n"
        f"💬 <i>Для ответа сделайте Reply на это сообщение.</i>"
    )

    if ADMIN_ID:
        try:
            if photo_id:
                await message.bot.send_photo(
                    chat_id=ADMIN_ID, photo=photo_id, caption=admin_text, parse_mode="HTML"
                )
            else:
                await message.bot.send_message(
                    chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Не удалось отправить обращение администратору: {e}")

    success_text = (
        "✅ <b>Мы получили ваше обращение</b>\n\n"
        "Специалист поддержки изучит ваш вопрос и ответит в ближайшее время."
    )
    await message.answer(success_text, reply_markup=get_back_home_keyboard(), parse_mode="HTML")
    await state.clear()


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
        await message.reply("❌ Не удалось распознать ID пользователя.")
        return

    try:
        answer_text = (
            "💬 <b>Ответ от поддержки:</b>\n\n"
            f"{message.text or message.caption or ''}"
        )
        await message.bot.send_message(chat_id=target_user_id, text=answer_text, parse_mode="HTML")
        await message.reply("✅ Ответ успешно отправлен пользователю.")
    except Exception as e:
        await message.reply(f"❌ Ошибка отправки: {e}")


# === ОПЛАТА ===
@router.callback_query(F.data == "go_to_payment")
async def process_go_to_payment(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    total_price = data.get("total_price", 10)

    text = (
        "💳 <b>Способ оплаты</b>\n\n"
        f"• Сумма к оплате: <b>{total_price}$ USDT</b>\n\n"
        "Выберите удобный вариант:"
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🤖 CryptoBot (мгновенно)",
                    callback_data="pay_cryptobot",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🪙 Криптовалюта (TRC-20 / BEP-20 / TON / BTC)",
                    callback_data="crypto_expert",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="catalog")],
        ]
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


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
    try:
        async with ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("ok"):
                        return data["result"]["pay_url"]
    except Exception as e:
        logger.error(f"Ошибка создания CryptoBot инвойса: {e}")
    return ""


@router.callback_query(F.data == "pay_cryptobot")
async def process_pay_cryptobot(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    total_price = data.get("total_price", 10)

    pay_url = await create_cryptobot_invoice(total_price, "Оплата киберуслуг")
    if not pay_url:
        await callback.answer("❌ Сервис оплаты временно недоступен.", show_alert=True)
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Оплатить счет", url=pay_url)],
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил(-а), проверить",
                    callback_data="cryptobot_check_success",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="go_to_payment")],
        ]
    )
    text = (
        "🤖 <b>Оплата через CryptoBot</b>\n\n"
        f"Сумма: <b>{total_price} USDT</b>\n\n"
        "После совершения платежа нажмите кнопку проверки ниже."
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "cryptobot_check_success")
async def process_cryptobot_check(callback: CallbackQuery, state: FSMContext):
    user = callback.from_user
    data = await state.get_data()

    service_name = data.get("service_name", "Услуга")
    total_price = data.get("total_price", 10)

    add_order_db(user.id, service_name, total_price, "⏳ Платёж проверяется")

    user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
    if user.full_name:
        user_info = f"{user.full_name} ({user_info})"

    admin_order_text = (
        f"🤖 <b>Новый платеж через CryptoBot</b>\n\n"
        f"• От кого: {user_info}\n"
        f"• User ID: <code>{user.id}</code>\n"
        f"• Услуга: <b>{service_name}</b>\n"
        f"{get_admin_order_details_text(data)}"
        f"• Сумма: <b>{total_price}$</b>\n\n"
        f"💬 <i>Сделайте Reply на это сообщение для ответа.</i>"
    )
    if ADMIN_ID:
        try:
            await callback.bot.send_message(chat_id=ADMIN_ID, text=admin_order_text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу: {e}")

    final_text = (
        "⏳ <b>Платёж проверяется</b>\n\n"
        "Система проверяет зачисление средств. Администратор подтвердит заявку в течение 25 минут."
    )
    await callback.message.edit_text(final_text, reply_markup=get_back_home_keyboard(), parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data == "crypto_expert")
async def process_crypto_expert(callback: CallbackQuery):
    text = "🪙 <b>Выберите криптовалюту / сеть:</b>"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔴 USDT (TRC-20)", callback_data="pay_trc20")],
            [InlineKeyboardButton(text="🟢 USDT (BEP-20)", callback_data="pay_bep20")],
            [InlineKeyboardButton(text="🔵 TON", callback_data="pay_ton")],
            [InlineKeyboardButton(text="🟠 Bitcoin (BTC)", callback_data="pay_btc")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="go_to_payment")],
        ]
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.in_({"pay_trc20", "pay_bep20", "pay_ton", "pay_btc"}))
async def process_crypto_wallet(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    total_price = data.get("total_price", 10)

    wallets = {
        "pay_trc20": ("USDT (TRC-20)", "<code>TYd1ekJgTHnFfgAVYXsi7689GaG5oZN3Xe</code>"),
        "pay_bep20": ("USDT (BEP-20)", "<code>0x0A6C6634f8FD920a5E9E0d241CD46d6C15377f39</code>"),
        "pay_ton": ("TON", "<code>UQDwk7PZoWr0zHv-BItBEGxnq4uyj81z6BlIw74heDgF-Shd</code>"),
        "pay_btc": ("Bitcoin (BTC)", "<code>bc1qj7xy0645vcza7l3et5s8nuahllua9a439c0795</code>"),
    }
    net_name, address = wallets[callback.data]

    text = (
        f"💳 <b>Оплата {net_name}</b>\n\n"
        f"• Сумма: <b>{total_price}$</b>\n"
        f"• Реквизиты:\n{address}\n\n"
        "После перевода отправьте чек (скриншот) в этот чат."
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить чек", callback_data="ask_for_receipt")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="crypto_expert")],
        ]
    )
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "ask_for_receipt")
async def ask_for_receipt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OrderForm.receipt)
    text = (
        "🧾 <b>Подтверждение платежа</b>\n\n"
        "Отправьте в чат <b>скриншот или фотографию чека</b> о переводе."
    )
    await callback.message.edit_text(text, reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    await callback.answer()


@router.message(OrderForm.receipt, F.photo)
async def process_receipt_photo(message: Message, state: FSMContext):
    user = message.from_user
    data = await state.get_data()

    service_name = data.get("service_name", "Услуга")
    total_price = data.get("total_price", 10)

    add_order_db(user.id, service_name, total_price, "⏳ Платёж проверяется")

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
            await message.bot.send_photo(
                chat_id=ADMIN_ID, photo=photo_id, caption=admin_order_text, parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить чек админу: {e}")

    final_text = (
        "⏳ <b>Платёж проверяется</b>\n\n"
        "Мы получили ваш чек и проверяем зачисление средств."
    )
    await message.answer(final_text, reply_markup=get_back_home_keyboard(), parse_mode="HTML")
    await state.clear()


@router.message(OrderForm.receipt, ~F.photo)
async def process_receipt_invalid(message: Message):
    await message.answer("📎 Пожалуйста, прикрепите <b>скриншот или фото чека картинкой</b>.")


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
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    asyncio.create_task(start_web_server())

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен пользователем.")
