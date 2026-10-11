# webapp/core/security.py
import os
from datetime import datetime, timedelta
from typing import Optional
from passlib.context import CryptContext
from jose import jwt

# Настройки
SECRET_KEY = os.getenv("SECRET_KEY", "CHANGE_THIS_TO_A_SUPER_SECRET_STRING")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 90  # 90 дней: вход в браузере не должен слетать каждую неделю

# ИЗМЕНЕНИЕ: Используем argon2 вместо bcrypt
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Проверка пароля (Argon2 не требует обрезки)"""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    """Хеширование пароля (Argon2 не требует обрезки)"""
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def set_session_cookie(response, access_token: str, request=None) -> None:
    """
    Cookie входа в кабинет. С `max_age` — переживает закрытие браузера (без него это
    сессионная cookie, и человека выкидывало бы при каждом перезапуске). Срок равен
    сроку жизни самого JWT.
    """
    response.set_cookie(
        key="access_token", value=f"Bearer {access_token}", httponly=True,
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60, samesite="lax",
        secure=bool(request is not None and request.url.scheme == "https"),
    )
