# tgbot/services/intro_offer.py
"""
Вводный тариф: «1 ₽ за 7 дней, дальше автоматически N ₽ в месяц».

Правила (чистые функции, без БД — их проверяют тесты):
  * продаётся один раз и только тем, кто ещё ни разу не платил;
  * покупается только с сохранением карты (без неё переход не состоится);
  * промокоды, доп. устройства и Telegram Stars к нему не применяются;
  * 1 ₽ первой оплатой не считается: `is_first_payment_made` и бонус рефереру
    появляются при первом списании полной цены тарифа продления;
  * тариф продления списывается в день окончания, а не за 3 дня, как обычное
    автопродление (иначе неделя превратилась бы в 4 дня).

«В intro-периоде» = `intro_used AND NOT is_first_payment_made` — отдельного
статуса в БД нет.
"""


def is_intro_eligible(user) -> bool:
    """Может ли пользователь купить вводный тариф."""
    if user is None:
        return False
    return not user.is_first_payment_made and not getattr(user, 'intro_used', False)


def is_in_intro(user) -> bool:
    """Пользователь оплатил вводный тариф и ещё не перешёл на полную цену."""
    if user is None:
        return False
    return bool(getattr(user, 'intro_used', False)) and not user.is_first_payment_made


def is_sellable_intro(tariff, tariffs_by_id: dict) -> bool:
    """Вводный тариф можно продавать, только если есть куда переводить:
    тариф продления существует, активен и сам не вводный."""
    if not getattr(tariff, 'is_intro', False) or not tariff.is_active:
        return False
    target = tariffs_by_id.get(tariff.renew_tariff_id)
    return target is not None and target.is_active and not getattr(target, 'is_intro', False)


def visible_tariffs(tariffs, user, all_tariffs_by_id: dict) -> list:
    """Витрина для конкретного пользователя: вводный тариф — первым и только
    тем, кому он положен; обычные тарифы — в исходном порядке."""
    eligible = is_intro_eligible(user)
    intro, regular = [], []
    for t in tariffs:
        if getattr(t, 'is_intro', False):
            if eligible and is_sellable_intro(t, all_tariffs_by_id):
                intro.append(t)
        else:
            regular.append(t)
    return intro + regular


def conversion_price(tariff) -> float:
    """Сколько списать при переходе с вводного тарифа: обычная цена тарифа
    продления — её мы обещали в тексте согласия, лоялти-цена тут ни при чём."""
    return tariff.price


def format_rub(amount: float) -> str:
    """99.0 → '99', 149.5 → '149.50'."""
    return f"{int(amount)}" if float(amount).is_integer() else f"{amount:.2f}"


def consent_text(intro_tariff, renew_tariff) -> str:
    """Текст согласия перед кнопкой оплаты — одинаковый в боте, вебе и Mini App."""
    return (
        f"{format_rub(intro_tariff.price)} ₽ за {intro_tariff.duration_days} дн. "
        f"Затем {format_rub(conversion_price(renew_tariff))} ₽ каждые "
        f"{renew_tariff.duration_days} дн. автоматически с привязанной карты. "
        f"Отключить автопродление можно в любой момент в профиле."
    )


def intro_block_reason(tariff, user, tariffs_by_id: dict) -> str | None:
    """Почему нельзя оформить этот тариф (None — можно). Серверная проверка
    в точках создания платежа: витрину можно обойти старой кнопкой."""
    if not tariff.is_active:
        return "Тариф больше не продаётся."
    if not getattr(tariff, 'is_intro', False):
        return None
    if not is_intro_eligible(user):
        return "Пробная неделя доступна только при первой покупке."
    if not is_sellable_intro(tariff, tariffs_by_id):
        return "Тариф временно недоступен."
    return None
