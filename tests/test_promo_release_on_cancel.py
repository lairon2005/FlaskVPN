# tests/test_promo_release_on_cancel.py
"""
Возврат промокода при отмене неоплаченного счёта (tgbot/services/payment_service.py).

Промокод захватывается до оплаты: в боте — при вводе, на сайте и в Mini App — в
POST /payment/create. Раньше отмена счёта (кнопкой, джобом автоотмены или
вебхуком payment.canceled) оставляла его израсходованным, и человек, передумавший
насчёт способа оплаты, терял скидку. Теперь:
- отмена возвращает промокод (_release_promo), если он не оплачен другим счётом;
- оплата отменённого счёта по старой ссылке захватывает промокод снова (_reclaim_promo);
- бот перед новым счётом убеждается, что промокод из FSM снова за пользователем (hold_promo);
- PromoCodeRepository.release_claim возвращает uses_left, только если отметка была.

Приёмы загрузки — те же, что в test_stale_payment_cancel.py.
"""
import asyncio
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from test_stars_payment import load_payment_service_module


def _payment(**overrides):
    defaults = {
        "yookassa_payment_id": "yk-1",
        "user_id": 42,
        "tariff_id": 5,
        "final_amount": 90.0,
        "status": "pending",
        "kind": "subscription",
        "extra_devices": 0,
        "source": "web",
        "discount_percent": 10,
        "promo_code": "SALE10",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _promo(**overrides):
    defaults = {"id": 7, "code": "SALE10", "discount_percent": 10, "bonus_days": 0, "expire_date": None}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _payment_repo(**overrides):
    repo = SimpleNamespace(
        get_user_pending=AsyncMock(return_value=_payment()),
        get_by_yookassa_id=AsyncMock(return_value=_payment()),
        get_pending_older_than=AsyncMock(return_value=[]),
        cancel_if_pending=AsyncMock(return_value=True),
        has_paid_with_promo=AsyncMock(return_value=False),
        update_status=AsyncMock(),
    )
    for name, value in overrides.items():
        setattr(repo, name, value)
    return repo


def _promo_service(promo=None):
    return SimpleNamespace(
        get_by_code=AsyncMock(return_value=promo if promo is not None else _promo()),
        is_claimed=AsyncMock(return_value=False),
        apply=AsyncMock(),
        release=AsyncMock(return_value=True),
    )


def _service(module, payment_repo, promo_service):
    return module.PaymentService(
        subscription_service=SimpleNamespace(
            extend=AsyncMock(return_value=SimpleNamespace(is_new_user=False, username="user_42"))
        ),
        referral_service=SimpleNamespace(process_first_payment_bonus=AsyncMock(return_value=None)),
        user_repo=SimpleNamespace(
            get=AsyncMock(return_value=SimpleNamespace(user_id=42, is_first_payment_made=True)),
            set_first_payment_done=AsyncMock(),
            set_intro_used=AsyncMock(),
        ),
        tariff_repo=SimpleNamespace(get_by_id=AsyncMock(return_value=SimpleNamespace(
            id=5, name="Месяц", duration_days=30, data_limit_gb=None,
            is_intro=False, renew_tariff_id=None,
        ))),
        payment_repo=payment_repo,
        promo_service=promo_service,
    )


# =============================================================================
# --- Отмена возвращает промокод ---
# =============================================================================


class ReleaseOnCancelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_payment_service_module()

    async def test_user_cancel_returns_promo(self):
        repo, promos = _payment_repo(), _promo_service()
        service = _service(self.module, repo, promos)

        cancelled = await service.cancel_pending_payment(42)

        self.assertEqual(cancelled.yookassa_payment_id, "yk-1")
        repo.cancel_if_pending.assert_awaited_once_with("yk-1")
        promos.get_by_code.assert_awaited_once_with("SALE10")
        promos.release.assert_awaited_once_with(42, promos.get_by_code.return_value)

    async def test_invoice_paid_before_cancel_keeps_promo(self):
        # Счёт оплатился между выборкой и отменой — отменять и возвращать нечего.
        repo = _payment_repo(cancel_if_pending=AsyncMock(return_value=False))
        promos = _promo_service()
        service = _service(self.module, repo, promos)

        self.assertIsNone(await service.cancel_pending_payment(42))
        promos.release.assert_not_awaited()

    async def test_invoice_without_promo_touches_nothing(self):
        repo = _payment_repo(get_user_pending=AsyncMock(return_value=_payment(promo_code=None)))
        promos = _promo_service()
        service = _service(self.module, repo, promos)

        await service.cancel_pending_payment(42)

        promos.get_by_code.assert_not_awaited()
        promos.release.assert_not_awaited()

    async def test_promo_already_paid_by_another_invoice_is_not_returned(self):
        # Бот держит скидку в FSM и после оплаты: отменяется уже второй счёт.
        repo = _payment_repo(has_paid_with_promo=AsyncMock(return_value=True))
        promos = _promo_service()
        service = _service(self.module, repo, promos)

        await service.cancel_pending_payment(42)

        repo.has_paid_with_promo.assert_awaited_once_with(42, "SALE10")
        promos.release.assert_not_awaited()

    async def test_release_failure_does_not_break_cancel(self):
        repo, promos = _payment_repo(), _promo_service()
        promos.release.side_effect = RuntimeError("db down")
        service = _service(self.module, repo, promos)

        cancelled = await service.cancel_pending_payment(42)

        self.assertIsNotNone(cancelled)

    async def test_service_without_promo_service_still_cancels(self):
        repo = _payment_repo()
        service = _service(self.module, repo, None)

        self.assertIsNotNone(await service.cancel_pending_payment(42))

    async def test_stale_cancel_returns_promo_only_for_marked(self):
        stale = [_payment(yookassa_payment_id="yk-1"), _payment(yookassa_payment_id="yk-2")]
        repo = _payment_repo(
            get_pending_older_than=AsyncMock(return_value=stale),
            # yk-2 успели оплатить по пути — его не трогаем.
            cancel_if_pending=AsyncMock(side_effect=lambda yk_id: yk_id == "yk-1"),
        )
        promos = _promo_service()
        service = _service(self.module, repo, promos)

        cancelled = await service.cancel_stale_payments(60)

        self.assertEqual([p.yookassa_payment_id for p in cancelled], ["yk-1"])
        promos.release.assert_awaited_once()

    async def test_gateway_cancel_returns_promo(self):
        repo, promos = _payment_repo(), _promo_service()
        service = _service(self.module, repo, promos)

        payment = await service.cancel_by_gateway("yk-1")

        self.assertEqual(payment.user_id, 42)
        repo.cancel_if_pending.assert_awaited_once_with("yk-1")
        promos.release.assert_awaited_once()

    async def test_gateway_cancel_of_already_cancelled_invoice_does_not_release_twice(self):
        repo = _payment_repo(cancel_if_pending=AsyncMock(return_value=False))
        promos = _promo_service()
        service = _service(self.module, repo, promos)

        payment = await service.cancel_by_gateway("yk-1")

        # Запись всё равно отдаём — по ней вебхук уведомляет пользователя.
        self.assertIsNotNone(payment)
        promos.release.assert_not_awaited()

    async def test_gateway_cancel_of_unknown_invoice(self):
        repo = _payment_repo(get_by_yookassa_id=AsyncMock(return_value=None))
        promos = _promo_service()
        service = _service(self.module, repo, promos)

        self.assertIsNone(await service.cancel_by_gateway("yk-404"))
        repo.cancel_if_pending.assert_not_awaited()


# =============================================================================
# --- Оплата отменённого счёта по старой ссылке ---
# =============================================================================


class ReclaimOnLatePaymentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_payment_service_module()

    def _repo(self, status):
        return _payment_repo(get_by_yookassa_id=AsyncMock(return_value=_payment(status=status)))

    async def test_paid_cancelled_invoice_claims_promo_again(self):
        repo, promos = self._repo("cancelled"), _promo_service()
        service = _service(self.module, repo, promos)

        result = await service.process_successful_payment("yk-1", 90.0)

        self.assertIsNotNone(result)
        promos.apply.assert_awaited_once_with(42, promos.get_by_code.return_value)

    async def test_promo_still_held_is_not_claimed_twice(self):
        repo, promos = self._repo("cancelled"), _promo_service()
        promos.is_claimed.return_value = True
        service = _service(self.module, repo, promos)

        await service.process_successful_payment("yk-1", 90.0)

        promos.apply.assert_not_awaited()

    async def test_exhausted_promo_does_not_block_payment(self):
        # Деньги уже списаны — подписку продлеваем, даже если промокод кончился.
        repo, promos = self._repo("cancelled"), _promo_service()
        promos.apply.side_effect = self.module.PromoClaimError()
        service = _service(self.module, repo, promos)

        result = await service.process_successful_payment("yk-1", 90.0)

        self.assertIsNotNone(result)
        service._subscription_service.extend.assert_awaited_once()

    async def test_regular_pending_payment_does_not_touch_promo(self):
        repo, promos = self._repo("pending"), _promo_service()
        service = _service(self.module, repo, promos)

        await service.process_successful_payment("yk-1", 90.0)

        promos.get_by_code.assert_not_awaited()
        promos.apply.assert_not_awaited()


# =============================================================================
# --- Бот: скидка из FSM только при захваченном промокоде ---
# =============================================================================


class HoldPromoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_payment_service_module()

    async def test_held_promo_is_used_as_is(self):
        repo, promos = _payment_repo(), _promo_service()
        promos.is_claimed.return_value = True
        service = _service(self.module, repo, promos)

        promo = await service.hold_promo(42, "SALE10")

        self.assertIs(promo, promos.get_by_code.return_value)
        promos.apply.assert_not_awaited()

    async def test_returned_promo_is_claimed_again(self):
        repo, promos = _payment_repo(), _promo_service()
        service = _service(self.module, repo, promos)

        promo = await service.hold_promo(42, "SALE10")

        self.assertIsNotNone(promo)
        promos.apply.assert_awaited_once_with(42, promo)

    async def test_promo_taken_by_others_meanwhile_gives_no_discount(self):
        repo, promos = _payment_repo(), _promo_service()
        promos.apply.side_effect = self.module.PromoClaimError()
        service = _service(self.module, repo, promos)

        self.assertIsNone(await service.hold_promo(42, "SALE10"))

    async def test_promo_already_paid_gives_no_discount(self):
        repo = _payment_repo(has_paid_with_promo=AsyncMock(return_value=True))
        promos = _promo_service()
        promos.is_claimed.return_value = True
        service = _service(self.module, repo, promos)

        self.assertIsNone(await service.hold_promo(42, "SALE10"))

    async def test_expired_promo_is_not_claimed_again(self):
        repo = _payment_repo()
        promos = _promo_service(_promo(expire_date=datetime.now() - timedelta(days=1)))
        service = _service(self.module, repo, promos)

        self.assertIsNone(await service.hold_promo(42, "SALE10"))
        promos.apply.assert_not_awaited()

    async def test_deleted_promo_gives_no_discount(self):
        repo, promos = _payment_repo(), _promo_service()
        promos.get_by_code.return_value = None
        service = _service(self.module, repo, promos)

        self.assertIsNone(await service.hold_promo(42, "SALE10"))


# =============================================================================
# --- PromoCodeRepository.release_claim: uses_left только при снятой отметке ---
# =============================================================================


def load_promo_repository_module():
    """database/repositories/promo_code.py с минимальными моделями вместо db.py.

    Модели настоящие SQLAlchemy — delete()/update() строятся по ним, а сессия
    фейковая: проверяем, какие запросы уходят, а не сам SQL.
    """
    from sqlalchemy import Integer, BigInteger, UniqueConstraint
    from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

    class Base(DeclarativeBase):
        pass

    class PromoCode(Base):
        __tablename__ = "promo_codes"
        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        uses_left: Mapped[int] = mapped_column(Integer)

    class UsedPromoCode(Base):
        __tablename__ = "used_promo_codes"
        __table_args__ = (UniqueConstraint("user_id", "promo_code_id", name="uq_used_promo_user_code"),)
        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        user_id: Mapped[int] = mapped_column(BigInteger)
        promo_code_id: Mapped[int] = mapped_column(Integer)

    db_module = types.ModuleType("db")
    db_module.PromoCode = PromoCode
    db_module.UsedPromoCode = UsedPromoCode

    path = Path(__file__).resolve().parents[1] / "database" / "repositories" / "promo_code.py"
    saved = sys.modules.get("db")
    sys.modules["db"] = db_module
    try:
        spec = importlib.util.spec_from_file_location("promo_repository_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if saved is None:
            sys.modules.pop("db", None)
        else:
            sys.modules["db"] = saved
    return module


class _FakeSession:
    def __init__(self, deleted_row):
        self._deleted_row = deleted_row
        self.statements = []
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def execute(self, stmt):
        self.statements.append(stmt)
        return Mock(first=Mock(return_value=self._deleted_row))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class ReleaseClaimRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.module = load_promo_repository_module()

    def _release(self, deleted_row):
        session = _FakeSession(deleted_row)
        repo = self.module.PromoCodeRepository(lambda: session)
        released = asyncio.run(repo.release_claim(42, SimpleNamespace(id=7)))
        return released, session

    def test_existing_claim_is_released_and_uses_left_returned(self):
        released, session = self._release(deleted_row=(1,))

        self.assertTrue(released)
        self.assertEqual(len(session.statements), 2)  # DELETE отметки + UPDATE uses_left
        session.commit.assert_awaited_once()

    def test_missing_claim_does_not_inflate_uses_left(self):
        # Повторная отмена или старый веб-счёт, созданный без захвата.
        released, session = self._release(deleted_row=None)

        self.assertFalse(released)
        self.assertEqual(len(session.statements), 1)  # только DELETE, без UPDATE
        session.rollback.assert_awaited_once()
        session.commit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
