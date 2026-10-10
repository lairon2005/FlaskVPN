# webapp/routers/auth.py
import hmac
import random
import logging
from jose import jwt
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, status, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, update

from db import async_session_maker, User
from tgbot.services import referral_service
from webapp.core.mail import send_reset_code, send_verification_email, MailSendError
from webapp.core.verification import generate_code, hash_code, check_code
from webapp.core.security import get_password_hash, verify_password, create_access_token, ACCESS_TOKEN_EXPIRE_MINUTES, SECRET_KEY, ALGORITHM
from webapp.dependencies import get_current_user
from webapp.templating import render
from datetime import timedelta, datetime

router = APIRouter()

MAX_RESET_ATTEMPTS = 5  # неверных вводов кода сброса, после чего код гасится

# Зависимость для получения сессии БД
async def get_db():
    async with async_session_maker() as session:
        yield session

def parse_ref(value) -> int | None:
    """?ref= из ссылки приглашения: id реферера (Telegram — положительный, сайт — отрицательный)."""
    try:
        ref = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return ref or None


# --- ГЕНЕРАЦИЯ ID ДЛЯ WEB ---
async def generate_web_user_id(session: AsyncSession) -> int:
    """
    Генерирует отрицательный ID, чтобы не конфликтовать с Telegram ID.
    Проверяет, свободен ли ID.
    """
    while True:
        # Генерируем ID от -1 до -1 000 000 000
        new_id = random.randint(-1000000000, -1)
        result = await session.execute(select(User).where(User.user_id == new_id))
        if not result.scalar_one_or_none():
            return new_id

# --- СТРАНИЦЫ (GET) ---

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, user: User = Depends(get_current_user)):
    if user:
        return RedirectResponse(url="/profile/", status_code=302)
    return render(request, "login.html", {})

@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, user: User = Depends(get_current_user)):
    if user:
        return RedirectResponse(url="/profile/", status_code=302)
    ref = request.query_params.get("ref")
    return render(request, "register.html", {"ref": ref})

@router.get("/logout")
async def logout_user():
    # Перенаправляем на страницу входа
    response = RedirectResponse(url="/login", status_code=302)
    # Удаляем куку с токеном
    response.delete_cookie(key="access_token")
    return response
@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return render(request, "forgot_password.html", {})

@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request):
    return render(request, "reset_password.html", {})

@router.post("/register")
async def register_user(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(...),
    ref: str = Form(None),
    db: AsyncSession = Depends(get_db)
):
    logging.info(f"Attempting to register user with email: {email}")

    # Валидация пароля
    if len(password) < 6:
        return render(request, "register.html", {
            "error": "Пароль должен содержать минимум 6 символов",
            "ref": ref,
        })

    # 1. Проверяем, есть ли такой email
    existing_user = await db.execute(select(User).where(User.email == email))
    if existing_user.scalar_one_or_none():
        return render(request, "register.html", {
            "error": "Пользователь с таким Email уже существует",
            "ref": ref,
        })

    # 2. Генерируем код верификации
    code = generate_code()
    hashed_password = get_password_hash(password)

    # 3. Создаём подписанный JWT с pending-данными (пользователь НЕ создаётся в БД)
    registration_token = jwt.encode({
        "type": "registration",
        "email": email,
        "full_name": full_name,
        "password_hash": hashed_password,
        "ref": ref,
        "code_hash": hash_code(email, code),
        "code_expire": (datetime.utcnow() + timedelta(minutes=15)).isoformat(),
        "code_sent_at": datetime.utcnow().isoformat(),
        "attempts": 0,
        "exp": datetime.utcnow() + timedelta(hours=1),
    }, SECRET_KEY, algorithm=ALGORITHM)

    # 4. Отправляем код на email
    try:
        await send_verification_email(email, code)
    except MailSendError as e:
        return render(request, "register.html", {
            "error": str(e),
            "ref": ref,
        })

    # 5. Рендерим страницу ввода кода
    return render(request, "verify_email.html", {
        "registration_token": registration_token,
        "email": email,
        "message": "Код подтверждения отправлен на вашу почту",
    })


@router.post("/verify-email")
async def verify_email(
    request: Request,
    registration_token: str = Form(...),
    code: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    # 1. Декодируем токен
    try:
        payload = jwt.decode(registration_token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return render(request, "register.html", {
            "error": "Токен регистрации недействителен. Пожалуйста, зарегистрируйтесь заново.",
        })

    if payload.get("type") != "registration":
        return render(request, "register.html", {
            "error": "Недействительный токен регистрации.",
        })

    # 2. Проверяем лимит попыток
    attempts = payload.get("attempts", 0)
    if attempts >= 5:
        return render(request, "register.html", {
            "error": "Превышено количество попыток. Пожалуйста, зарегистрируйтесь заново.",
        })

    # 3. Проверяем срок действия кода
    code_expire = datetime.fromisoformat(payload["code_expire"])
    if datetime.utcnow() > code_expire:
        return render(request, "verify_email.html", {
            "registration_token": registration_token,
            "email": payload["email"],
            "error": "Срок действия кода истёк. Запросите новый код.",
        })

    # 4. Проверяем код
    if not check_code(payload["email"], code, payload.get("code_hash", "")):
        # Создаём новый токен с увеличенным счётчиком попыток
        payload["attempts"] = attempts + 1
        new_token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
        return render(request, "verify_email.html", {
            "registration_token": new_token,
            "email": payload["email"],
            "error": f"Неверный код. Осталось попыток: {5 - payload['attempts']}",
        })

    # 5. Код верный — создаём пользователя
    # Повторная проверка email (мог быть занят за время ввода кода)
    existing = await db.execute(select(User).where(User.email == payload["email"]))
    if existing.scalar_one_or_none():
        return render(request, "register.html", {
            "error": "Пользователь с таким Email уже существует",
        })

    new_user_id = await generate_web_user_id(db)

    new_user = User(
        user_id=new_user_id,
        email=payload["email"],
        password_hash=payload["password_hash"],
        full_name=payload["full_name"],
        username=payload["email"].split('@')[0],
        has_received_trial=False,
        is_email_verified=True,
    )

    db.add(new_user)
    await db.commit()

    # Реферал — тем же сервисом, что и /start в боте: проверка, что реферер существует,
    # и пометка «друг активного партнёра» (с его оплат партнёру пойдут проценты).
    referrer_id = parse_ref(payload.get("ref"))
    if referrer_id is not None:
        try:
            await referral_service.attach_referrer(new_user.user_id, referrer_id)
        except Exception:
            # Регистрация важнее приглашения: сбой привязки не должен оставить человека без входа.
            logging.exception(f"Web register: failed to attach referrer {referrer_id} to {new_user.user_id}")

    # 6. Логиним и редиректим
    access_token = create_access_token(
        data={"sub": str(new_user.user_id), "email": new_user.email},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )

    response = RedirectResponse(url="/profile/", status_code=302)
    response.set_cookie(key="access_token", value=f"Bearer {access_token}", httponly=True)
    return response


@router.post("/resend-code")
async def resend_code(
    request: Request,
    registration_token: str = Form(...),
):
    # 1. Декодируем токен
    try:
        payload = jwt.decode(registration_token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return render(request, "register.html", {
            "error": "Токен регистрации недействителен. Зарегистрируйтесь заново.",
        })

    if payload.get("type") != "registration":
        return render(request, "register.html", {
            "error": "Недействительный токен.",
        })

    # 2. Rate-limit: не чаще 1 раза в 2 минуты
    code_sent_at = datetime.fromisoformat(payload.get("code_sent_at", "2000-01-01"))
    if (datetime.utcnow() - code_sent_at).total_seconds() < 120:
        remaining = 120 - int((datetime.utcnow() - code_sent_at).total_seconds())
        return render(request, "verify_email.html", {
            "registration_token": registration_token,
            "email": payload["email"],
            "error": f"Подождите {remaining} сек. перед повторной отправкой кода.",
        })

    # 3. Генерируем новый код
    new_code = generate_code()

    # 4. Создаём новый токен
    payload["code_hash"] = hash_code(payload["email"], new_code)
    payload["code_expire"] = (datetime.utcnow() + timedelta(minutes=15)).isoformat()
    payload["code_sent_at"] = datetime.utcnow().isoformat()
    payload["attempts"] = 0
    payload["exp"] = datetime.utcnow() + timedelta(hours=1)
    new_token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

    # 5. Отправляем
    try:
        await send_verification_email(payload["email"], new_code)
    except MailSendError as e:
        return render(request, "verify_email.html", {
            "registration_token": registration_token,
            "email": payload["email"],
            "error": str(e),
        })

    return render(request, "verify_email.html", {
        "registration_token": new_token,
        "email": payload["email"],
        "message": "Новый код отправлен на вашу почту!",
    })

@router.post("/login")
async def login_user(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    # 1. Ищем пользователя
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    
    # 2. Проверка пароля
    if not user or not user.password_hash or not verify_password(password, user.password_hash):
        return render(request, "login.html", {
            "error": "Неверный Email или пароль",
        })
    
    # 3. Создаем токен
    access_token = create_access_token(
        data={"sub": str(user.user_id), "email": user.email},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    
    response = RedirectResponse(url="/", status_code=302)
    # httponly=True защищает от кражи кук через JS
    response.set_cookie(key="access_token", value=f"Bearer {access_token}", httponly=True)
    return response


@router.post("/forgot-password")
async def send_reset_email(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    # 1. Ищем пользователя
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user:
        # Для безопасности можно писать "Если почта существует, мы отправили код", 
        # но для удобства скажем правду
        return render(request, "forgot_password.html", {
            "error": "Пользователь с таким Email не найден",
        })

    # 2. Генерируем код (6 цифр)
    code = generate_code()
    
    # 3. Сохраняем в БД (время жизни 15 мин)
    user.reset_code = code
    user.reset_code_expire = datetime.now() + timedelta(minutes=15)
    user.reset_attempts = 0
    await db.commit()

    # 4. Отправляем письмо
    try:
        await send_reset_code(email, code)
    except MailSendError as e:
        return render(request, "forgot_password.html", {
            "error": str(e),
        })

    # 5. Перенаправляем на ввод кода
    return render(request, "reset_password.html", {
        "email": email,  # Передаем email, чтобы юзеру не вводить его снова
        "message": "Код отправлен на вашу почту!",
    })


@router.post("/reset-password")
async def process_reset_password(
    request: Request,
    email: str = Form(...),
    code: str = Form(...),
    new_password: str = Form(...),
    db: AsyncSession = Depends(get_db)
):
    # 0. Валидация пароля
    if len(new_password) < 6:
        return render(request, "reset_password.html", {
            "email": email, "error": "Пароль должен содержать минимум 6 символов",
        })

    # 1. Ищем пользователя
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user:
        return render(request, "reset_password.html", {
            "email": email, "error": "Пользователь не найден",
        })

    # 2. Код должен быть выдан и не истёк
    if not user.reset_code or not user.reset_code_expire or user.reset_code_expire < datetime.now():
        return render(request, "reset_password.html", {
            "email": email, "error": "Код не запрашивался или срок его действия истёк. Запросите новый.",
        })

    # 3. Счётчик попыток. Инкремент атомарный (UPDATE ... RETURNING), чтобы параллельные
    # запросы не могли перебирать код в обход лимита; считается каждая попытка, включая последнюю.
    stored_code = user.reset_code
    attempts = (await db.execute(
        update(User).where(User.user_id == user.user_id)
        .values(reset_attempts=User.reset_attempts + 1)
        .returning(User.reset_attempts)
        .execution_options(synchronize_session=False)
    )).scalar_one()

    if attempts > MAX_RESET_ATTEMPTS:
        user.reset_code = None
        user.reset_code_expire = None
        await db.commit()
        return render(request, "reset_password.html", {
            "email": email, "error": "Превышено количество попыток. Запросите новый код.",
        })

    # bytes: compare_digest на str падает с TypeError при не-ASCII вводе
    if not hmac.compare_digest(stored_code.encode(), code.strip().encode()):
        await db.commit()
        return render(request, "reset_password.html", {
            "email": email,
            "error": f"Неверный код. Осталось попыток: {MAX_RESET_ATTEMPTS - attempts}",
        })

    # 3. Меняем пароль
    user.password_hash = get_password_hash(new_password)
    
    # 4. Очищаем код (чтобы нельзя было использовать повторно)
    user.reset_code = None
    user.reset_code_expire = None
    user.reset_attempts = 0
    await db.commit()

    # 5. Отправляем на логин
    return render(request, "login.html", {
        "message": "Пароль успешно изменен! Теперь войдите.",
    })