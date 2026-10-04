"""
Партнёры — денежная рефералка вместо бонусных дней.

Админ выборочно включает её конкретным пользователям (как менеджеров). У партнёра:
  • друг по его ссылке получает тот же триал, что и обычно, а сам партнёр дней не получает;
  • с каждой рублёвой оплаты друга — процент на баланс (правила — partner_rules.py);
  • начисление сначала лежит в холде (partner_hold_days), потом становится доступным;
  • возврат оплаты друга гасит начисление (в холде — целиком, после — с баланса, но не ниже нуля);
  • баланс выводится заявкой по СБП (админ переводит руками и отмечает) или тратится
    на свою подписку целиком;
  • в ежемесячном лидерборде рефералки партнёр не участвует.

Отключённый партнёр перестаёт получать начисления (в том числе с уже приглашённых
друзей), но накопленный баланс можно вывести или потратить.

Сервис — бизнес-логика без Telegram; доставку сообщений делает `notifier`
(PartnerNotifier), сбой доставки деньги не откатывает.
"""
import datetime
from dataclasses import dataclass

from loader import logger
from tgbot.services.partner_rules import (
    DEFAULT_HOLD_DAYS, DEFAULT_MIN_WITHDRAWAL, DEFAULT_PERCENT,
    SETTING_HOLD_DAYS, SETTING_MIN_WITHDRAWAL, SETTING_PERCENT,
    PartnerSettings, commission_kop, counts_for_partner, fmt_rub, normalize_bank, normalize_phone, rub_to_kop,
)
from tgbot.services.pricing import effective_price

ACTIVE = "active"

ERRORS = {
    "not_partner": "Партнёрская программа для вас не подключена.",
    "user_not_found": "Пользователь не найден. Он должен хотя бы раз запустить бота.",
    "not_telegram": "Партнёром может быть только пользователь Telegram.",
    "bad_phone": "Не похоже на номер телефона. Пример: +7 999 123-45-67",
    "bad_bank": "Название банка — от 2 до 64 символов.",
    "no_requisites": "Сначала укажите реквизиты для СБП.",
    "below_min": "Сумма меньше минимальной для вывода.",
    "insufficient": "На балансе недостаточно средств.",
    "pending_exists": "У вас уже есть заявка на вывод — дождитесь решения по ней.",
    "already_resolved": "Заявка уже обработана.",
    "not_found": "Заявка не найдена.",
    "tariff_unavailable": "Этот тариф сейчас недоступен.",
    "payment_failed": "Не удалось провести оплату — деньги вернулись на баланс.",
    "bad_percent": "Ставка — целое число от 1 до 100.",
}


class PartnerError(Exception):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message or ERRORS.get(code, code)
        super().__init__(self.message)


@dataclass
class PartnerOverview:
    user_id: int
    active: bool
    percent: int
    personal_percent: bool
    balance_kop: int
    hold_kop: int
    next_release: datetime.datetime | None
    invited: int
    paid_friends: int
    earned_kop: int
    withdrawn_kop: int
    sbp_phone: str | None
    sbp_bank: str | None
    pending_withdrawal: object | None
    settings: PartnerSettings

    @property
    def has_requisites(self) -> bool:
        return bool(self.sbp_phone and self.sbp_bank)

    @property
    def can_withdraw(self) -> bool:
        return self.pending_withdrawal is None and self.balance_kop >= self.settings.min_withdrawal_kop


@dataclass
class BalanceOffer:
    tariff: object
    total_kop: int
    quote: object
    affordable: bool


class PartnerService:
    def __init__(self, partner_repo, user_repo, settings_repo, tariff_repo=None, payment_service=None):
        self._repo = partner_repo
        self._users = user_repo
        self._settings = settings_repo
        self._tariffs = tariff_repo
        self._payments = payment_service
        self.notifier = None

    # --- настройки и чтение -------------------------------------------------

    async def settings(self) -> PartnerSettings:
        return PartnerSettings(
            percent=await self._settings.get_int(SETTING_PERCENT, DEFAULT_PERCENT),
            hold_days=await self._settings.get_int(SETTING_HOLD_DAYS, DEFAULT_HOLD_DAYS),
            min_withdrawal=await self._settings.get_int(SETTING_MIN_WITHDRAWAL, DEFAULT_MIN_WITHDRAWAL),
        )

    async def get(self, user_id: int):
        return await self._repo.get(user_id)

    async def is_active(self, user_id: int) -> bool:
        return await self._repo.is_active(user_id)

    async def overview(self, user_id: int) -> PartnerOverview | None:
        partner = await self._repo.get(user_id)
        if partner is None:
            return None
        settings = await self.settings()
        summary = await self._repo.summary(user_id)
        return PartnerOverview(
            user_id=user_id, active=partner.status == ACTIVE,
            percent=partner.percent or settings.percent, personal_percent=partner.percent is not None,
            balance_kop=int(partner.balance_kop or 0), hold_kop=summary["hold_kop"],
            next_release=summary["next_release"], invited=summary["invited"],
            paid_friends=summary["paid_friends"], earned_kop=summary["earned_kop"],
            withdrawn_kop=summary["withdrawn_kop"], sbp_phone=partner.sbp_phone, sbp_bank=partner.sbp_bank,
            pending_withdrawal=await self._repo.pending_withdrawal(user_id), settings=settings,
        )

    async def history(self, user_id: int, limit: int = 15):
        return await self._repo.history(user_id, limit)

    # --- конвейер оплат -------------------------------------------------------

    async def on_payment_succeeded(self, payment):
        """Процент партнёру с оплаты друга. Идемпотентно: повтор вебхука ничего не начислит."""
        if not counts_for_partner(payment.kind, payment.source):
            return None
        friend = await self._users.get(payment.user_id)
        if friend is None or not friend.referrer_id or not getattr(friend, "partner_referred", False):
            return None
        partner = await self._repo.get(friend.referrer_id)
        if partner is None or partner.status != ACTIVE:
            return None

        base_kop = rub_to_kop(payment.final_amount or 0)
        if getattr(payment, "manager_id", None):
            base_kop -= rub_to_kop(await self._repo.manager_fee_for_payment(payment.yookassa_payment_id))
        settings = await self.settings()
        percent = partner.percent or settings.percent
        amount_kop = commission_kop(base_kop, percent)
        if amount_kop <= 0:
            return None

        available_at = datetime.datetime.now() + datetime.timedelta(days=max(0, settings.hold_days))
        entry = await self._repo.add_accrual(
            partner_id=partner.user_id, friend_id=friend.user_id, payment_id=payment.yookassa_payment_id,
            base_kop=base_kop, percent=percent, amount_kop=amount_kop, available_at=available_at,
        )
        if entry is None:
            return None
        logger.info(
            f"[partner] +{fmt_rub(amount_kop)} партнёру {partner.user_id} с оплаты {payment.yookassa_payment_id} "
            f"друга {friend.user_id} ({percent}% от {fmt_rub(base_kop)}), доступно с {available_at:%d.%m.%Y}"
        )
        await self._notify("accrual", partner.user_id, entry)
        return entry

    async def on_payment_refunded(self, payment):
        """Возврат оплаты друга: начисление гасится, баланс ниже нуля не уходит."""
        result = await self._repo.reverse_payment(payment.yookassa_payment_id)
        if result is None:
            return None
        outcome, amount_kop, entry = result
        logger.info(f"[partner] возврат {payment.yookassa_payment_id}: {outcome} {fmt_rub(amount_kop)}, "
                    f"партнёр {entry.partner_user_id}")
        await self._notify("reversal", entry.partner_user_id, outcome, amount_kop, entry)
        return result

    async def release_holds(self) -> int:
        """Джоб: начисления с истёкшим холдом → на баланс. Партнёру — одно сообщение на всё сразу."""
        released = await self._repo.release_due(datetime.datetime.now())
        per_partner: dict[int, int] = {}
        for entry in released:
            per_partner[entry.partner_user_id] = per_partner.get(entry.partner_user_id, 0) + entry.amount_kop
        for partner_id, total in per_partner.items():
            partner = await self._repo.get(partner_id)
            await self._notify("released", partner_id, total, int(partner.balance_kop or 0) if partner else total)
        return len(released)

    # --- вывод -------------------------------------------------------------------

    async def set_requisites(self, user_id: int, phone_text: str, bank_text: str) -> tuple[str, str]:
        if await self._repo.get(user_id) is None:
            raise PartnerError("not_partner")
        phone = normalize_phone(phone_text)
        if phone is None:
            raise PartnerError("bad_phone")
        bank = normalize_bank(bank_text)
        if bank is None:
            raise PartnerError("bad_bank")
        await self._repo.set_requisites(user_id, phone, bank)
        return phone, bank

    async def request_withdrawal(self, user_id: int, amount_kop: int):
        partner = await self._repo.get(user_id)
        if partner is None:
            raise PartnerError("not_partner")
        if not (partner.sbp_phone and partner.sbp_bank):
            raise PartnerError("no_requisites")
        settings = await self.settings()
        if amount_kop < settings.min_withdrawal_kop:
            raise PartnerError("below_min", f"Минимальная сумма вывода — {fmt_rub(settings.min_withdrawal_kop)}.")
        if await self._repo.pending_withdrawal(user_id) is not None:
            raise PartnerError("pending_exists")
        withdrawal = await self._repo.create_withdrawal(user_id, amount_kop, partner.sbp_phone, partner.sbp_bank)
        if withdrawal is None:
            raise PartnerError("insufficient")
        logger.info(f"[partner] заявка на вывод #{withdrawal.id}: {fmt_rub(amount_kop)}, партнёр {user_id}")
        user = await self._users.get(user_id)
        posted = await self._notify("withdrawal_created", withdrawal, user)
        if posted:
            await self._repo.set_withdrawal_message(withdrawal.id, *posted)
        return withdrawal

    async def resolve_withdrawal(self, withdrawal_id: int, admin_id: int, *, paid: bool, reason: str | None = None):
        withdrawal = await self._repo.resolve_withdrawal(withdrawal_id, paid=paid, admin_id=admin_id, reason=reason)
        if withdrawal is None:
            existing = await self._repo.get_withdrawal(withdrawal_id)
            raise PartnerError("already_resolved" if existing else "not_found")
        logger.info(f"[partner] заявка #{withdrawal_id} → {withdrawal.status} (админ {admin_id})")
        user = await self._users.get(withdrawal.partner_user_id)
        await self._notify("withdrawal_resolved", withdrawal, user)
        return withdrawal

    async def get_withdrawal(self, withdrawal_id: int):
        return await self._repo.get_withdrawal(withdrawal_id)

    async def list_pending_withdrawals(self):
        return await self._repo.list_pending_withdrawals()

    # --- оплата подписки с баланса ------------------------------------------

    async def balance_offers(self, user_id: int) -> list[BalanceOffer]:
        """Тарифы и суммы для оплаты с баланса (тариф + уже купленные слоты и трафик)."""
        partner = await self._repo.get(user_id)
        if partner is None:
            raise PartnerError("not_partner")
        user = await self._users.get(user_id)
        offers = []
        for tariff in await self._tariffs.get_active():
            quote = await self._quote(user, tariff)
            total_kop = rub_to_kop(quote.total)
            offers.append(BalanceOffer(tariff=tariff, total_kop=total_kop, quote=quote,
                                       affordable=total_kop <= int(partner.balance_kop or 0)))
        return offers

    async def _quote(self, user, tariff):
        end = user.subscription_end_date if user else None
        has_active = bool(end and end > datetime.datetime.now())
        return await self._payments.renewal_quote(user, tariff, effective_price(tariff, has_active))

    async def pay_with_balance(self, user_id: int, tariff_id: int, nonce: str):
        """
        Оплата своей подписки с баланса — только целиком. nonce из кнопки подтверждения
        делает операцию идемпотентной: двойное нажатие не спишет деньги дважды.
        Возвращает (PaymentResult | None, replayed).
        """
        partner = await self._repo.get(user_id)
        if partner is None:
            raise PartnerError("not_partner")
        tariff = await self._tariffs.get_by_id(tariff_id)
        if tariff is None or not tariff.is_active or tariff.is_intro:
            raise PartnerError("tariff_unavailable")

        pay_id = f"balance:{user_id}:{nonce}"
        if await self._payments.get_payment(pay_id) is not None:
            return None, True

        user = await self._users.get(user_id)
        quote = await self._quote(user, tariff)
        amount_kop = rub_to_kop(quote.total)
        if not await self._repo.debit(user_id, amount_kop, "spend", payment_id=pay_id):
            if await self._payments.get_payment(pay_id) is not None:
                return None, True
            raise PartnerError("insufficient")

        result = None
        try:
            await self._payments.create_payment_record(
                yookassa_payment_id=pay_id, user_id=user_id, tariff_id=tariff.id,
                original_amount=quote.total, final_amount=quote.total, source="balance", kind="subscription",
                extra_devices=quote.slots, extra_traffic_gb=quote.traffic_gb,
            )
            result = await self._payments.process_successful_payment(pay_id, quote.total)
        except Exception:
            logger.error(f"[partner] оплата с баланса упала: {pay_id}", exc_info=True)

        if result is None:
            stored = await self._payments.get_payment(pay_id)
            if stored is None or stored.status != "succeeded":
                if stored is not None:
                    await self._payments.mark_payment_failed(pay_id)
                await self._repo.credit(user_id, amount_kop, "spend_return", payment_id=pay_id)
                raise PartnerError("payment_failed")
            return None, True

        logger.info(f"[partner] оплата с баланса: партнёр {user_id}, {tariff.name}, {fmt_rub(amount_kop)}")
        await self._notify("balance_payment", user, tariff, amount_kop)
        return result, False

    # --- админка -------------------------------------------------------------------

    async def resolve_user(self, ident: str):
        """Пользователь по Telegram ID или @username."""
        ident = (ident or "").strip()
        if ident.lstrip("-").isdigit():
            user = await self._users.get(int(ident))
        else:
            user = await self._users.get_by_username(ident.lstrip("@"))
        if user is None:
            raise PartnerError("user_not_found")
        return user

    async def add_partner(self, ident: str, admin_id: int):
        user = await self.resolve_user(ident)
        if user.user_id <= 0:
            raise PartnerError("not_telegram")
        partner = await self._repo.enable(user.user_id, admin_id)
        logger.info(f"[partner] партнёрка включена пользователю {user.user_id} админом {admin_id}")
        return partner, user

    async def enable(self, user_id: int, admin_id: int):
        partner = await self._repo.enable(user_id, admin_id)
        logger.info(f"[partner] партнёрка включена пользователю {user_id} админом {admin_id}")
        return partner

    async def disable(self, user_id: int, admin_id: int) -> bool:
        changed = await self._repo.disable(user_id)
        if changed:
            logger.info(f"[partner] партнёрка отключена пользователю {user_id} админом {admin_id}")
        return changed

    async def set_percent(self, user_id: int, percent: int | None) -> None:
        if percent is not None and not 1 <= percent <= 100:
            raise PartnerError("bad_percent")
        await self._repo.set_percent(user_id, percent)

    async def list_partners(self):
        return await self._repo.list_with_users()

    # --- доставка -----------------------------------------------------------------

    async def _notify(self, event: str, *args):
        if self.notifier is None:
            return None
        try:
            return await getattr(self.notifier, event)(*args)
        except Exception as e:
            logger.warning(f"[partner] уведомление {event} не доставлено: {e}")
            return None
