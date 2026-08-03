"""JWT auth — pbkdf2_sha256, safe dev mode without FK violation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from astra.config import settings
from astra.api.deps import db_session
from astra.db.models import User

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

ZERO_UUID = UUID(int=0)


def is_zero_uuid(uid) -> bool:
    try:
        return str(uid) == str(ZERO_UUID) or uid == ZERO_UUID or str(uid) == "00000000-0000-0000-0000-000000000000"
    except Exception:
        return False


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password[:1024])


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=settings.jwt_expire_minutes))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


async def get_user_by_username(db: AsyncSession, username: str) -> Optional[User]:
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: UUID) -> Optional[User]:
    return await db.get(User, user_id)


async def authenticate_user(db: AsyncSession, username: str, password: str) -> Optional[User]:
    user = await get_user_by_username(db, username)
    if not user:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


def get_safe_owner_id(user: Optional[User]) -> Optional[UUID]:
    """Return None if user is None or zero UUID (dev mode), else user.id."""
    if not user:
        return None
    try:
        uid = getattr(user, "id", None)
        if not uid:
            return None
        if is_zero_uuid(uid):
            return None
        return uid
    except Exception:
        return None


async def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(db_session),
) -> User:
    if not token:
        if settings.auth_enabled:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
        # Dev mode without auth: try to return first real user, else ephemeral dev user that won't be persisted as FK
        result = await db.execute(select(User).limit(1))
        first = result.scalar_one_or_none()
        if first:
            return first
        # Ephemeral dev user with zero UUID — get_safe_owner_id will turn it into None for FK
        return User(
            id=ZERO_UUID,
            username="dev",
            email="dev@astra.local",
            hashed_password="",
            is_active=True,
        )

    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")

    user = await get_user_by_username(db, username)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Inactive user")
    return user


async def get_current_user_optional(
    token: Optional[str] = Depends(oauth2_scheme),
    db: AsyncSession = Depends(db_session),
) -> Optional[User]:
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        username: str = payload.get("sub")
        if not username:
            return None
    except JWTError:
        return None
    user = await get_user_by_username(db, username)
    return user
