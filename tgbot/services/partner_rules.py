"""
Правила денежной рефералки (партнёры) — чистые функции без БД и Telegram.

С каждой рублёвой оплаты приглашённого друга партнёру начисляется процент:
покупка и продление тарифа (в том числе автосписание, вместе со слотами и трафиком
на чекауте) и «свои дни» у менеджера. Не считаются: отдельная докупка слотов и
трафика, Telegram Stars (не рубли) и оплата с самого партнёрского баланса.
Услуга менеджера в продаже — деньги менеджера, процент с неё не берётся.
"""
import re
from dataclasses import dataclass

SETTING_PERCENT = "partner_percent"
SETTING_HOLD_DAYS = "partner_hold_days"
SETTING_MIN_WITHDRAWAL = "partner_min_withdrawal"

DEFAULT_PERCENT = 30
DEFAULT_HOLD_DAYS = 14
DEFAULT_MIN_WITHDRAWAL = 500   # ₽

ACCRUAL_KINDS = ("subscription", "custom")
EXCLUDED_SOURCES = ("stars", "balance")


@dataclass(frozen=True)
class PartnerSettings:
    percent: int = DEFAULT_PERCENT
    hold_days: int = DEFAULT_HOLD_DAYS
    min_withdrawal: int = DEFAULT_MIN_WITHDRAWAL   # ₽

    @property
    def min_withdrawal_kop(self) -> int:
        return self.min_withdrawal * 100


def counts_for_partner(kind: str | None, source: str | None) -> bool:
    """Идёт ли эта оплата друга в зачёт партнёру."""
    return (kind or "subscription") in ACCRUAL_KINDS and (source or "bot") not in EXCLUDED_SOURCES


def rub_to_kop(amount: float) -> int:
    return int(round(float(amount) * 100))


def commission_kop(base_kop: int, percent: int) -> int:
    """Процент от суммы в копейках, с округлением вниз — копейку в пользу магазина."""
    if base_kop <= 0 or percent <= 0:
        return 0
    return base_kop * percent // 100


def fmt_rub(kop: int) -> str:
    """12345 → '123,45 ₽', 50000 → '500 ₽'."""
    sign = "−" if kop < 0 else ""
    kop = abs(int(kop))
    rub, rest = divmod(kop, 100)
    whole = f"{rub:,}".replace(",", " ")
    return f"{sign}{whole},{rest:02d} ₽" if rest else f"{sign}{whole} ₽"


def parse_rub(text: str) -> int | None:
    """'1 500', '1500,5', '1500.50 ₽' → копейки. None — не число."""
    cleaned = (text or "").replace("₽", "").replace(" ", "").replace(" ", "").replace(",", ".").strip()
    if not re.fullmatch(r"\d{1,9}(\.\d{1,2})?", cleaned):
        return None
    return rub_to_kop(float(cleaned))


def normalize_phone(text: str) -> str | None:
    """Телефон для СБП → '+79991234567'. Российские 8/7/9… приводятся к +7. None — не телефон."""
    digits = re.sub(r"\D", "", text or "")
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    if 11 <= len(digits) <= 15 and (text or "").strip().startswith("+"):
        return "+" + digits
    return None


def normalize_bank(text: str) -> str | None:
    bank = " ".join((text or "").split())
    return bank if 2 <= len(bank) <= 64 else None
