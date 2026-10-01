"""
Цена подписки на произвольное число дней (офлайн-продажа менеджером).

Формула (все слагаемые не убывают по d, поэтому цена монотонна — больше дней
никогда не стоит меньше):

    цена(d) = округление_до_…9( max(
        мин_чек,
        цена_дня × (d + K × √d),            база + надбавка за короткий срок
        max{цена тарифа T : дни(T) ≤ d},    не дешевле более короткого тарифа
        d × min(цена(T) / дни(T)),          не дешевле лучшего тарифа за день
    ))

Зачем так:
  * каждый «свой» день дороже дня в стандартном тарифе: надбавка K·√d покрывает
    фиксированные затраты короткой продажи (время менеджера, комиссия эквайринга)
    и плавно уходит к нулю — без ступенек и дыр на границах тарифов;
  * не бывает инверсии «29 дней дороже, чем 30» и «свои 20 дней дороже тарифа
    на 30» — полы по тарифам этого не допускают;
  * менеджер видит ближайший стандартный тариф с тем же или большим сроком по
    меньшей цене — так клиента естественно вести на тариф с автопродлением.

Всё — чистые функции без БД: границы и монотонность проверяют тесты.
"""
from dataclasses import dataclass
from math import ceil, sqrt

# Ключи в таблице app_settings
SETTING_DAY_PRICE = "custom_day_price"
SETTING_SHORT_PREMIUM = "custom_short_premium"
SETTING_MIN_PRICE = "custom_min_price"
SETTING_MAX_DAYS = "custom_max_days"
SETTING_RENEW_TARIFF = "custom_renew_tariff_id"

DEFAULT_DAY_PRICE = 5.0
DEFAULT_SHORT_PREMIUM = 1.5
DEFAULT_MIN_PRICE = 59
DEFAULT_MAX_DAYS = 365


@dataclass(frozen=True)
class CustomPriceSettings:
    """Снимок настроек на момент расчёта (цена не должна меняться между экраном и счётом)."""
    day_price: float = DEFAULT_DAY_PRICE
    short_premium: float = DEFAULT_SHORT_PREMIUM
    min_price: int = DEFAULT_MIN_PRICE
    max_days: int = DEFAULT_MAX_DAYS


@dataclass(frozen=True)
class TariffRef:
    """Минимум о стандартном тарифе, нужный для расчёта: что входит в пол и подсказку."""
    name: str
    days: int
    price: float


@dataclass(frozen=True)
class TariffHint:
    """Стандартный тариф, который клиенту выгоднее своих дней."""
    name: str
    days: int
    price: float
    saving: float


@dataclass(frozen=True)
class CustomPrice:
    days: int
    price: int
    raw: float
    base: float
    premium: float
    floor_reason: str          # formula | min_price | tariff_floor | best_rate
    hint: TariffHint | None = None

    @property
    def per_day(self) -> float:
        return self.price / self.days

    def breakdown(self, day_price: float) -> str:
        """Строка для экрана менеджера: «45 × 5 ₽ + надбавка 50 ₽ = 275 → 279 ₽»."""
        premium = f" + надбавка {self.premium:.0f} ₽" if self.premium else ""
        reasons = {
            "formula": "",
            "min_price": " (минимальный чек)",
            "tariff_floor": " (не дешевле тарифа на меньший срок)",
            "best_rate": " (не дешевле лучшего тарифа за день)",
        }
        return (
            f"{self.days} × {day_price:g} ₽{premium} = {self.raw:.0f} → "
            f"{self.price} ₽{reasons.get(self.floor_reason, '')}"
        )


class CustomDaysError(ValueError):
    """Число дней вне допустимого диапазона."""


def ceil_to_9(value: float) -> int:
    """Наименьшее целое n ≥ value с последней цифрой 9 (274.2 → 279, 279 → 279)."""
    n = ceil(value)
    return n + ((9 - n % 10) % 10)


def validate_days(settings: CustomPriceSettings, days: int) -> int:
    if not isinstance(days, int) or isinstance(days, bool):
        raise CustomDaysError("Количество дней должно быть целым числом.")
    if days < 1:
        raise CustomDaysError("Минимум — 1 день.")
    if days > settings.max_days:
        raise CustomDaysError(f"Максимум — {settings.max_days} дн. Для долгого срока выберите тариф.")
    return days


def nearest_better_tariff(days: int, price: int, tariffs: list[TariffRef]) -> TariffHint | None:
    """Ближайший по сроку тариф на ≥ days, который не дороже своих дней."""
    candidates = [t for t in tariffs if t.days >= days and t.price <= price]
    if not candidates:
        return None
    best = min(candidates, key=lambda t: (t.days, t.price))
    return TariffHint(best.name, best.days, best.price, price - best.price)


def compute_custom_price(settings: CustomPriceSettings, days: int,
                         tariffs: list[TariffRef]) -> CustomPrice:
    """Цена `days` своих дней. `tariffs` — активные обычные (не вводные) тарифы по базовой цене."""
    days = validate_days(settings, days)

    base = settings.day_price * days
    premium = settings.day_price * settings.short_premium * sqrt(days)
    formula = base + premium

    shorter = [t.price for t in tariffs if t.days <= days]
    tariff_floor = max(shorter) if shorter else 0.0
    rates = [t.price / t.days for t in tariffs if t.days > 0]
    best_rate_floor = days * min(rates) if rates else 0.0

    candidates = {
        "formula": formula,
        "min_price": float(settings.min_price),
        "tariff_floor": tariff_floor,
        "best_rate": best_rate_floor,
    }
    # При равенстве приоритет у формулы: объяснение «по формуле» понятнее менеджеру.
    reason = max(candidates, key=lambda k: (candidates[k], k == "formula"))
    raw = candidates[reason]

    price = ceil_to_9(raw)
    return CustomPrice(
        days=days,
        price=price,
        raw=raw,
        base=base,
        premium=premium if reason == "formula" else 0.0,
        floor_reason=reason,
        hint=nearest_better_tariff(days, price, tariffs),
    )


def preview_table(settings: CustomPriceSettings, tariffs: list[TariffRef],
                  days_list: tuple[int, ...] = (1, 3, 7, 14, 30, 60, 90, 180, 365)) -> list[CustomPrice]:
    """Таблица-превью для админки: как цена выглядит при текущих настройках."""
    return [
        compute_custom_price(settings, d, tariffs)
        for d in days_list if d <= settings.max_days
    ]
