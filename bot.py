# bot.py

import asyncio
import logging
from typing import List

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from config import BOT_TOKEN, PUBLIC_BASE_URL, HH_USER_AGENT
from db_models import AsyncSessionLocal, User, Notification, UserRequestLog

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# Фразы, по которым считаем сообщение отказом
REJECTION_PATTERNS = [
    "к сожалению",
    "к сожелению",   # частая опечатка
    "мы не готовы вас принять",
    "вы нам не подходите",
    "вынуждены отказать",
    "отказ",
    "не сможем продолжить",
]


async def log_user_request(tg_id: int, text: str):
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select

        res = await session.execute(
            select(User).where(User.telegram_id == tg_id)
        )
        user = res.scalar_one_or_none()

        req = UserRequestLog(
            user_id=user.id if user else None,
            telegram_id=tg_id,
            message_text=text,
        )
        session.add(req)
        await session.commit()


@dp.message(Command("start"))
async def cmd_start(message: Message):
    tg_id = message.from_user.id
    await log_user_request(tg_id, message.text or "")

    # создаём пользователя, если его нет
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select

        res = await session.execute(
            select(User).where(User.telegram_id == tg_id)
        )
        user = res.scalar_one_or_none()
        if not user:
            user = User(telegram_id=tg_id)  # mute_rejections=True по умолчанию
            session.add(user)
            await session.commit()

    auth_link = f"{PUBLIC_BASE_URL}/hh/auth/start?tg_id={tg_id}"
    text = (
        "Привет! 👋\n\n"
        "Я бот для уведомлений с hh.ru.\n\n"
        "1. Нажми на ссылку ниже и авторизуйся через hh.ru, чтобы привязать аккаунт:\n"
        f"{auth_link}\n\n"
        "2. После привязки я буду присылать уведомления о приглашениях и новых сообщениях.\n"
        "По умолчанию сообщения с отказами я <b>не присылаю</b>. Это можно настроить командой /settings."
    )
    await message.answer(text)


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    tg_id = message.from_user.id
    await log_user_request(tg_id, message.text or "")

    async with AsyncSessionLocal() as session:
        from sqlalchemy import select

        res = await session.execute(
            select(User).where(User.telegram_id == tg_id)
        )
        user = res.scalar_one_or_none()
        if not user:
            await message.answer("Сначала отправь /start.")
            return

        # переключаем флаг "не уведомлять об отказах"
        user.mute_rejections = not user.mute_rejections
        await session.commit()

        if user.mute_rejections:
            await message.answer(
                "✅ Режим <b>НЕ уведомлять об отказах</b> включён.\n"
                "Я буду присылать только приглашения и нейтральные сообщения."
            )
        else:
            await message.answer(
                "ℹ️ Режим <b>НЕ уведомлять об отказах</b> выключен.\n"
                "Теперь буду присылать и отказные сообщения тоже."
            )


@dp.message()
async def any_message(message: Message):
    """
    Просто логируем все сообщения пользователя.
    """
    tg_id = message.from_user.id
    await log_user_request(tg_id, message.text or "")

    await message.answer(
        "Команда не распознана.\n"
        "Используй /start для привязки hh.ru или /settings для настроек."
    )


# === Фоновая задача: рассылка уведомлений из таблицы notifications ===

async def notifications_worker():
    """
    Периодически забирает из БД все неотправленные уведомления и шлёт их пользователям.
    Учитывает флаг mute_rejections.
    """
    from sqlalchemy import select

    while True:
        try:
            async with AsyncSessionLocal() as session:
                res = await session.execute(
                    select(Notification, User)
                    .join(User, User.id == Notification.user_id)
                    .where(Notification.sent == False)
                    .order_by(Notification.created_at)
                )
                rows: List[tuple[Notification, User]] = res.all()

                for notif, user in rows:
                    # если это отказ и у юзера включено не уведомлять об отказах — просто помечаем как отправленное
                    if notif.is_rejection and user.mute_rejections:
                        notif.sent = True
                        continue

                    try:
                        await bot.send_message(user.telegram_id, notif.text)
                        notif.sent = True
                    except Exception as e:
                        logger.exception("Failed to send notification: %s", e)

                await session.commit()

        except Exception as e:
            logger.exception("notifications_worker error: %s", e)

        await asyncio.sleep(5)  # интервал опроса таблицы уведомлений


# === Фоновая задача: опрос HH на новые сообщения в чатах ===

async def hh_messages_worker():
    """
    Периодически опрашивает HH API на новые сообщения в переговорах.
    Вебхуков для сообщений нет, поэтому только опрос.

    Схема (очень упрощённо):
      1. Получаем список переговоров /negotiations.
      2. По тем, где есть непрочитанные, запрашиваем /negotiations/{nid}/messages?with_text_only=true
      3. Для каждого сообщения, которое ещё не было сохранено и не похоже на отказ —
         создаём Notification(kind="message", is_rejection=...).
    """
    from sqlalchemy import select

    while True:
        try:
            async with AsyncSessionLocal() as session:
                res = await session.execute(
                    select(User).where(User.hh_access_token.is_not(None))
                )
                users = res.scalars().all()

            for user in users:
                if not user.hh_access_token:
                    continue

                headers = {
                    "Authorization": f"Bearer {user.hh_access_token}",
                    "HH-User-Agent": HH_USER_AGENT,
                }

                async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
                    # 1. список переговоров (эндпоинт см. в openapi hh: /negotiations)
                    try:
                        resp = await client.get("https://api.hh.ru/negotiations")
                        resp.raise_for_status()
                    except Exception as e:
                        logger.exception("Failed to fetch negotiations for user %s: %s", user.id, e)
                        continue

                    negotiations = resp.json().get("items", [])

                    async with AsyncSessionLocal() as session:
                        from sqlalchemy import select as sa_select

                        for neg in negotiations:
                            nid = neg.get("id") or neg.get("topic_id")
                            if not nid:
                                continue

                            # 2. сообщения по переговорам
                            try:
                                r_msgs = await client.get(
                                    f"https://api.hh.ru/negotiations/{nid}/messages",
                                    params={"with_text_only": True},
                                )
                                r_msgs.raise_for_status()
                            except Exception as e:
                                logger.exception("Failed to fetch messages for negotiation %s: %s", nid, e)
                                continue

                            msgs = r_msgs.json().get("items", [])

                            for msg in msgs:
                                msg_id = str(msg.get("id"))
                                text = (msg.get("text") or "").strip()
                                author_me = msg.get("author", {}).get("me", False)

                                # интересуют только входящие сообщения
                                if author_me or not text:
                                    continue

                                # уже есть такое уведомление?
                                res_notif = await session.execute(
                                    sa_select(Notification).where(
                                        Notification.user_id == user.id,
                                        Notification.kind == "message",
                                        Notification.hh_object_id == msg_id,
                                    )
                                )
                                existing = res_notif.scalar_one_or_none()
                                if existing:
                                    continue

                                # определяем, похоже ли на отказ по ключевым фразам
                                t_low = text.lower()
                                is_rej = any(p in t_low for p in REJECTION_PATTERNS)

                                notif = Notification(
                                    user_id=user.id,
                                    kind="message",
                                    hh_object_id=msg_id,
                                    text=f"💬 Новое сообщение на hh.ru:\n\n{text}",
                                    is_rejection=is_rej,
                                )
                                session.add(notif)

                        await session.commit()

        except Exception as e:
            logger.exception("hh_messages_worker error: %s", e)

        # например, раз в минуту
        await asyncio.sleep(60)


async def main():
    # поднимаем фоновые воркеры
    asyncio.create_task(notifications_worker())
    asyncio.create_task(hh_messages_worker())

    # запускаем бота (polling)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
