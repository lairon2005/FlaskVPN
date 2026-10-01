"""Чеки: обязательные поля журнала, три адресата, отсутствие секретов в групповом чеке."""
import importlib.util
import unittest
from datetime import datetime
from pathlib import Path

_path = Path(__file__).resolve().parents[1] / "tgbot" / "services" / "manager_receipts.py"
_spec = importlib.util.spec_from_file_location("manager_receipts_under_test", _path)
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)

SECRET_URL = "https://sub.example.com/sub/SECRETTOKEN123"


def _data(**overrides):
    base = dict(
        op_id=123, created_at=datetime(2026, 10, 1, 11, 32), manager_id=7, manager_name="Иван Петров",
        kind="tariff", status="completed", client_code="K7F3Q2", client_is_new=True,
        product_title="Месяц", days=30, traffic_gb=500, devices_limit=5,
        expires_at=datetime(2026, 10, 31, 11, 32), price=149.0, payment_method="online",
        autorenew=True, key_username="off_k7f3q2", key_fingerprint="3f9a", subscription_url=SECRET_URL,
    )
    base.update(overrides)
    return mr.ReceiptData(**base)


class FormatTests(unittest.TestCase):
    def test_receipt_number(self):
        self.assertEqual(mr.receipt_number(123), "M-000123")
        self.assertEqual(mr.receipt_number(1234567), "M-1234567")

    def test_money(self):
        self.assertEqual(mr.fmt_money(149), "149 ₽")
        self.assertEqual(mr.fmt_money(1250), "1 250 ₽")
        self.assertEqual(mr.fmt_money(149.5), "149,50 ₽")

    def test_time_is_moscow(self):
        # наивное время в БД — UTC: 11:32 UTC = 14:32 МСК
        self.assertEqual(mr.fmt_dt(datetime(2026, 10, 1, 11, 32)), "01.10.2026 14:32")


class LogFieldsTests(unittest.TestCase):
    """ТЗ п.9: в чеке есть ID менеджера, время, тип, клиент, тариф/дни, срок, стоимость, ключ."""

    def test_group_receipt_has_all_required_fields(self):
        text = mr.format_group_receipt(_data())
        for expected in ("M-000123", "#7", "01.10.2026 14:32", "K7F3Q2", "Месяц", "30 дн.",
                         "31.10.2026", "149 ₽", "off_k7f3q2", "3f9a"):
            self.assertIn(expected, text)

    def test_custom_days_show_price_breakdown(self):
        text = mr.format_group_receipt(_data(kind="custom", product_title="Свои дни", days=45,
                                             custom_note="45 × 5 ₽ + надбавка 50 ₽ = 275 → 279 ₽"))
        self.assertIn("Свои дни", text)
        self.assertIn("45 дн.", text)
        self.assertIn("надбавка", text)

    def test_cash_shows_outstanding(self):
        text = mr.format_group_receipt(_data(payment_method="cash", autorenew=None, cash_outstanding=1250))
        self.assertIn("Наличные", text)
        self.assertIn("1 250 ₽", text)

    def test_online_shows_autorenew(self):
        self.assertIn("автопродление ✅", mr.format_group_receipt(_data()))
        self.assertIn("автопродление ❌", mr.format_group_receipt(_data(autorenew=False)))


class PrivacyTests(unittest.TestCase):
    def test_group_receipt_never_contains_subscription_url(self):
        text = mr.format_group_receipt(_data())
        self.assertNotIn("SECRETTOKEN", text)
        self.assertNotIn("http", text)

    def test_manager_receipt_has_the_link(self):
        self.assertIn(SECRET_URL, mr.format_manager_receipt(_data()))

    def test_manager_receipt_hides_link_of_deleted_temp_key(self):
        text = mr.format_manager_receipt(_data(kind="temp", temp_deleted_at=datetime(2026, 10, 1, 12, 35),
                                               expires_at=datetime(2026, 10, 1, 12, 32)))
        self.assertNotIn(SECRET_URL, text)

    def test_no_link_before_the_key_is_issued(self):
        self.assertNotIn(SECRET_URL, mr.format_manager_receipt(_data(status="pending_payment")))

    def test_group_receipt_shows_only_short_manager_name(self):
        self.assertIn("Иван П.", mr.format_group_receipt(_data()))
        self.assertNotIn("Петров", mr.format_group_receipt(_data()))

    def test_html_is_escaped(self):
        text = mr.format_group_receipt(_data(manager_name="<b>Хакер</b>", product_title="<i>x</i>"))
        self.assertNotIn("<b>Хакер", text)
        self.assertIn("&lt;i&gt;", text)


class TempKeyTests(unittest.TestCase):
    def _temp(self, **kw):
        return _data(kind="temp", product_title="", days=None, price=0.0, payment_method="free",
                     autorenew=None, traffic_gb=5, devices_limit=1,
                     created_at=datetime(2026, 10, 1, 11, 35), expires_at=datetime(2026, 10, 1, 12, 35), **kw)

    def test_shows_lifetime_and_free(self):
        text = mr.format_group_receipt(self._temp())
        self.assertIn("Временный ключ", text)
        self.assertIn("14:35 → 15:35", text)
        self.assertIn("бесплатно", text)

    def test_deleted_line_is_appended_to_the_same_receipt(self):
        text = mr.format_group_receipt(self._temp(temp_deleted_at=datetime(2026, 10, 1, 12, 35)))
        self.assertIn("🗑 Ключ удалён 15:35", text)
        self.assertNotIn("✅ Ключ выдан", text)

    def test_temp_receipt_has_no_total(self):
        self.assertNotIn("Итого", mr.format_group_receipt(self._temp()))


class StatusTests(unittest.TestCase):
    def test_pending_and_failed(self):
        self.assertIn("Ждём оплату", mr.format_group_receipt(_data(status="pending_payment")))
        self.assertIn("ошибка", mr.format_group_receipt(_data(status="failed")))
        self.assertIn("Отменено", mr.format_group_receipt(_data(status="cancelled")))


class ClientReceiptTests(unittest.TestCase):
    def test_short_and_has_sum_but_no_internal_data(self):
        text = mr.format_client_receipt(_data())
        self.assertIn("149 ₽", text)
        self.assertIn("M-000123", text)
        self.assertNotIn("Менеджер", text)
        self.assertNotIn("SECRETTOKEN", text)


if __name__ == "__main__":
    unittest.main()
