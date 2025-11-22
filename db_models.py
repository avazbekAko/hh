# db_models.py

from __future__ import annotations

import datetime as dt
import os
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config import DATABASE_URL_ASYNC, DATABASE_URL_SYNC


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)

    # из HH
    hh_user_id: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    hh_access_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hh_refresh_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hh_expires_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # настройка: НЕ уведомлять об отказах (по умолчанию включено = True)
    mute_rejections: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    notifications: Mapped[list["Notification"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    requests: Mapped[list["UserRequestLog"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class Notification(Base):
    """
    Уведомление, которое нужно (или уже было) отправить пользователю в Telegram.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    user: Mapped[User] = relationship(back_populates="notifications")

    # invitation / message / state_change
    kind: Mapped[str] = mapped_column(String(32), nullable=False)

    # Идентификатор сущности HH (например, message_id, topic_id, vacancy_id и т.п.)
    hh_object_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)

    text: Mapped[str] = mapped_column(Text, nullable=False)

    # Отказ ли по смыслу (по этапу/тексту)
    is_rejection: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )

    # отправлено ли уже в Telegram
    sent: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        index=True,
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )


class UserRequestLog(Base):
    """
    Логи всех запросов пользователя боту.
    """

    __tablename__ = "user_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    user: Mapped[Optional[User]] = relationship(back_populates="requests")

    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )


class LogEvent(Base):
    """
    Отдельная таблица для логов приложения, чтобы они не мешались с рабочими таблицами.
    """

    __tablename__ = "log_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )


# === async engine / session ===

async_engine = create_async_engine(DATABASE_URL_ASYNC, echo=False, future=True)
AsyncSessionLocal = async_sessionmaker(async_engine, expire_on_commit=False)


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


# Для init_db.py пригодится sync-URL
DB_URL_SYNC = DATABASE_URL_SYNC
