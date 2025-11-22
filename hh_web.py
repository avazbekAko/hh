# hh_web.py

import datetime as dt
import logging
from typing import Any, Dict

import httpx
from fastAPI import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from config import (
    HH_CLIENT_ID,
    HH_CLIENT_SECRET,
    HH_REDIRECT_URI,
    HH_WEBHOOK_URL,
    HH_USER_AGENT,
)
from db_models import AsyncSessionLocal, User, Notification, LogEvent

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="HH OAuth & Webhook service")


# === Pydantic-схемы под вебхук ===

class WebhookEvent(BaseModel):
    id: str
    subscription_id: str
    action_type: str   # NEW_RESPONSE_OR_INVITATION_VACANCY / NEGOTIATION_EMPLOYER_STATE_CHANGE / ...
    user_id: str       # id пользователя HH (менеджер)
    payload: Dict[str, Any]


# === Вспомогательные функции ===

async def log_event(level: str, message: str, details: dict | None = None):
    """
    Лог в отдельную таблицу log_events, чтобы не забивать консоль и не мусорить в бизнес-таблицах.
    """
    async with AsyncSessionLocal() as session:
        log_row = LogEvent(level=level, message=message, details=details)
        session.add(log_row)
        await session.commit()


async def exchange_code_for_token(code: str) -> dict:
    """
    Обмен authorization_code на access/refresh токены:
    POST https://api.hh.ru/token
    """
    data = {
        "grant_type": "authorization_code",
        "client_id": HH_CLIENT_ID,
        "client_secret": HH_CLIENT_SECRET,
        "code": code,
        "redirect_uri": HH_REDIRECT_URI,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post("https://api.hh.ru/token", data=data)
        if resp.status_code >= 400:
            await log_event("ERROR", "Failed to exchange code for token", {"status": resp.status_code, "text": resp.text})
            raise HTTPException(status_code=500, detail="Failed to exchange code for token")
        return resp.json()


async def get_hh_me(access_token: str) -> dict:
    """
    Получить информацию о текущем пользователе (нужен hh_user_id).
    GET https://api.hh.ru/me
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "HH-User-Agent": HH_USER_AGENT,
    }
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        resp = await client.get("https://api.hh.ru/me")
        resp.raise_for_status()
        return resp.json()


async def subscribe_webhooks(access_token: str):
    """
    Подписка на нужные события HH:
      - NEW_RESPONSE_OR_INVITATION_VACANCY    (новые отклики/приглашения)
      - NEGOTIATION_EMPLOYER_STATE_CHANGE     (смена этапа отклика: в том числе отказы)
    POST https://api.hh.ru/webhook/subscriptions
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "HH-User-Agent": HH_USER_AGENT,
    }
    body = {
        "url": HH_WEBHOOK_URL,
        "actions": [
            {"type": "NEW_RESPONSE_OR_INVITATION_VACANCY"},
            {"type": "NEGOTIATION_EMPLOYER_STATE_CHANGE"},
        ],
    }
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        resp = await client.post("https://api.hh.ru/webhook/subscriptions", json=body)
        if resp.status_code >= 400:
            await log_event("ERROR", "Failed to subscribe webhooks", {"status": resp.status_code, "text": resp.text})
            raise HTTPException(status_code=500, detail="Failed to subscribe webhooks")


def is_rejection_state(to_state: str) -> bool:
    """
    Хелпер для определения "отказного" статуса по полю to_state.
    Тут можно допилить конкретные ID состояний из документации (discard / rejected и т.п.).
    Пока — простая эвристика.
    """
    s = to_state.lower()
    bad_keywords = ["discard", "rejected", "decline", "отказ", "закрыто", "завершено"]
    return any(k in s for k in bad_keywords)


# === OAuth ===

@app.get("/hh/auth/start")
async def hh_auth_start(tg_id: int):
    """
    Старт авторизации HH.
    Бот отдаёт пользователю ссылку вида:
      {PUBLIC_BASE_URL}/hh/auth/start?tg_id=<telegram_id>
    Здесь редиректим на https://hh.ru/oauth/authorize
    """
    auth_url = (
        "https://hh.ru/oauth/authorize"
        f"?response_type=code"
        f"&client_id={HH_CLIENT_ID}"
        f"&redirect_uri={HH_REDIRECT_URI}"
        f"&state={tg_id}"
    )
    return RedirectResponse(auth_url)


@app.get("/hh/oauth/callback")
async def hh_oauth_callback(
    code: str | None = None,
    error: str | None = None,
    state: str | None = None,
):
    if error:
        await log_event("ERROR", "HH OAuth error", {"error": error})
        return PlainTextResponse(f"HH authorization error: {error}", status_code=400)

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    tg_id = int(state)

    token_data = await exchange_code_for_token(code)
    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in")

    me = await get_hh_me(access_token)
    hh_user_id = str(me.get("id"))

    # сохраняем токены и hh_user_id в БД
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select

        res = await session.execute(
            select(User).where(User.telegram_id == tg_id)
        )
        user = res.scalar_one_or_none()
        if not user:
            user = User(telegram_id=tg_id)
            session.add(user)

        user.hh_user_id = hh_user_id
        user.hh_access_token = access_token
        user.hh_refresh_token = refresh_token
        if expires_in:
            user.hh_expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=expires_in)

        await session.commit()

    await subscribe_webhooks(access_token)
    await log_event("INFO", "HH account linked", {"tg_id": tg_id, "hh_user_id": hh_user_id})

    return PlainTextResponse("Ваш аккаунт hh.ru успешно привязан. Можно закрыть это окно и вернуться в бота.")


# === Webhook от HH ===

@app.post("/hh/webhook")
async def hh_webhook(request: Request):
    data = await request.json()
    event = WebhookEvent(**data)
    await log_event("INFO", "Incoming HH webhook", {"action_type": event.action_type, "user_id": event.user_id})

    # находим пользователя по hh_user_id
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select

        res = await session.execute(
            select(User).where(User.hh_user_id == event.user_id)
        )
        user = res.scalar_one_or_none()
        if not user:
            # неизвестный пользователь — игнорируем
            await log_event("WARNING", "Webhook for unknown hh_user_id", {"hh_user_id": event.user_id})
            return PlainTextResponse("unknown user", status_code=200)

        notif_text = ""
        kind = ""
        is_rej = False
        hh_object_id = None

        if event.action_type == "NEW_RESPONSE_OR_INVITATION_VACANCY":
            kind = "invitation"
            payload = event.payload
            vacancy_id = payload.get("vacancy_id")
            resume_id = payload.get("resume_id")
            hh_object_id = payload.get("topic_id") or payload.get("chat_id")

            notif_text = (
                "📩 Новое приглашение / отклик на hh.ru\n"
                f"vacancy_id: {vacancy_id}\n"
                f"resume_id: {resume_id}"
            )
            is_rej = False

        elif event.action_type == "NEGOTIATION_EMPLOYER_STATE_CHANGE":
            kind = "state_change"
            payload = event.payload
            from_state = payload.get("from_state")
            to_state = payload.get("to_state")
            vacancy_id = payload.get("vacancy_id")
            resume_id = payload.get("resume_id")
            transferred_at = payload.get("transferred_at")
            hh_object_id = payload.get("topic_id")

            is_rej = is_rejection_state(str(to_state))
            notif_text = (
                "📂 Изменение этапа отклика на hh.ru\n"
                f"vacancy_id: {vacancy_id}\n"
                f"resume_id: {resume_id}\n"
                f"{from_state} ➜ {to_state} ({transferred_at})"
            )

        else:
            # другие типы событий нам неинтересны
            return PlainTextResponse("ignored", status_code=200)

        # создаём запись уведомления — бот потом сам разошлёт
        notif = Notification(
            user_id=user.id,
            kind=kind,
            hh_object_id=hh_object_id,
            text=notif_text,
            is_rejection=is_rej,
        )
        session.add(notif)
        await session.commit()

    return PlainTextResponse("ok", status_code=200)
