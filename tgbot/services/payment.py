# tgbot/services/payment.py (или webapp/core/payment.py)
import uuid
from yookassa import Configuration, Payment
from yookassa.domain.notification import WebhookNotification

import uuid
from yookassa import Configuration, Payment

# Позиция чека: описание, количество и цена ЗА ЕДИНИЦУ.
# Список позиций нужен из-за доп. устройств: тариф и слоты — разные услуги,
# и по 54-ФЗ они обязаны быть отдельными строками чека, иначе нечего
# возвращать при частичном возврате и расходится фискализация.
def build_receipt_items(description: str, amount: float, items: list[dict] | None = None) -> list[dict]:
    """items → позиции чека YooKassa. None/пустой список → одна позиция на всю сумму."""
    if not items:
        items = [{"description": description, "quantity": 1, "amount": amount}]

    receipt_items = []
    for item in items:
        quantity = item.get("quantity", 1)
        receipt_items.append({
            # ЮKassa режет описание позиции на 128 символах — обрезаем сами,
            # иначе Payment.create падает с ошибкой валидации.
            "description": str(item["description"])[:128],
            "quantity": f"{float(quantity):.2f}",
            "amount": {"value": f"{float(item['amount']):.2f}", "currency": "RUB"},
            "vat_code": "1",
            "payment_mode": "full_prepayment",
            "payment_subject": "service",
        })
    return receipt_items


def create_payment(
    amount: float,
    description: str,
    return_url: str,
    user_id: int,
    user_email: str = None,
    metadata: dict = None,
    shop_id: str = None,     # Передаем явно
    secret_key: str = None,  # Передаем явно
    save_payment_method: bool = False,
    payment_method_type: str = None,  # 'bank_card' | 'sbp' — фиксируем способ оплаты
    items: list[dict] = None,  # позиции чека; None → одна позиция на всю сумму
):
    """
    Универсальная функция создания платежа в YooKassa со всеми полями JSON.
    """
    # Настраиваем SDK перед запросом
    if shop_id and secret_key:
        Configuration.account_id = shop_id
        Configuration.secret_key = secret_key

    idempotence_key = str(uuid.uuid4())

    # Email для чека (обязательно хотя бы один контакт)
    receipt_email = user_email if user_email else f"user_{abs(user_id)}@telegram.user"

    # Данные для чека (receipt). vat_code=1 — НДС не облагается.
    receipt_data = {
        "customer": {"email": receipt_email},
        "items": build_receipt_items(description, amount, items),
    }

    # Метаданные платежа
    final_metadata = {'user_id': str(user_id)}
    if metadata:
        final_metadata.update(metadata)

    # Тело запроса
    payment_body = {
        "amount": {
            "value": str(amount),   # Сумма платежа
            "currency": "RUB"
        },
        "confirmation": {
            "type": "redirect",
            "return_url": return_url
        },
        "capture": True,
        "description": description,
        "metadata": final_metadata,
        "receipt": receipt_data,
    }

    # Сохранение метода оплаты (для автопродления). Включается только когда
    # рекурренты одобрены магазином ЮKassa — иначе Payment.create вернёт ошибку.
    if save_payment_method:
        payment_body["save_payment_method"] = True

    # Фиксируем конкретный способ оплаты (карта / СБП). ЮKassa покажет только его
    # и привяжет именно этот метод для последующих автосписаний.
    if payment_method_type:
        payment_body["payment_method_data"] = {"type": payment_method_type}

    # Создаем платеж
    payment_obj = Payment.create(payment_body, idempotence_key)

    # Возвращаем ссылку на оплату и ID платежа
    return payment_obj.confirmation.confirmation_url, payment_obj.id


def create_recurring_payment(
    amount: float,
    description: str,
    payment_method_id: str,
    user_id: int,
    user_email: str = None,
    metadata: dict = None,
    shop_id: str = None,
    secret_key: str = None,
    items: list[dict] = None,  # позиции чека; None → одна позиция на всю сумму
):
    """Списание по сохранённому методу оплаты (merchant-initiated, без подтверждения).
    Возвращает (yookassa_payment_id, status)."""
    if shop_id and secret_key:
        Configuration.account_id = shop_id
        Configuration.secret_key = secret_key

    idempotence_key = str(uuid.uuid4())

    receipt_email = user_email if user_email else f"user_{abs(user_id)}@telegram.user"

    receipt_data = {
        "customer": {"email": receipt_email},
        "items": build_receipt_items(description, amount, items),
    }

    final_metadata = {'user_id': str(user_id)}
    if metadata:
        final_metadata.update(metadata)

    payment_obj = Payment.create({
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "payment_method_id": payment_method_id,
        "description": description,
        "metadata": final_metadata,
        "receipt": receipt_data,
    }, idempotence_key)

    return payment_obj.id, payment_obj.status

def get_payment_url(yookassa_payment_id: str, shop_id: str = None, secret_key: str = None) -> str | None:
    """Retrieve the confirmation URL for an existing YooKassa payment."""
    if shop_id and secret_key:
        Configuration.account_id = shop_id
        Configuration.secret_key = secret_key

    payment_obj = Payment.find_one(yookassa_payment_id)
    if payment_obj and payment_obj.confirmation:
        return payment_obj.confirmation.confirmation_url
    return None


def parse_webhook_notification(request_body: dict):
    try:
        return WebhookNotification(request_body)
    except Exception:
        return None