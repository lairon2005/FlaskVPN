"""Цена «своих дней»: монотонность, полы по тарифам, округление, подсказка."""
import importlib.util
import unittest
from pathlib import Path

_path = Path(__file__).resolve().parents[1] / "tgbot" / "services" / "custom_pricing.py"
_spec = importlib.util.spec_from_file_location("custom_pricing_under_test", _path)
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

TARIFFS = [
    cp.TariffRef("Неделя", 7, 74),
    cp.TariffRef("Месяц", 30, 149),
    cp.TariffRef("3 месяца", 90, 399),
]
S = cp.CustomPriceSettings()


def price(days, tariffs=TARIFFS, settings=S):
    return cp.compute_custom_price(settings, days, tariffs).price


class RoundingTests(unittest.TestCase):
    def test_rounds_up_to_nine(self):
        self.assertEqual(cp.ceil_to_9(274.2), 279)
        self.assertEqual(cp.ceil_to_9(279), 279)
        self.assertEqual(cp.ceil_to_9(279.01), 289)
        self.assertEqual(cp.ceil_to_9(1), 9)

    def test_always_ends_with_nine(self):
        for d in range(1, 366):
            self.assertEqual(price(d) % 10, 9, d)


class MonotonicTests(unittest.TestCase):
    def test_more_days_never_cost_less(self):
        prices = [price(d) for d in range(1, 366)]
        for a, b, d in zip(prices, prices[1:], range(1, 366)):
            self.assertLessEqual(a, b, f"инверсия на {d} → {d + 1} дн.")

    def test_monotonic_for_odd_settings_too(self):
        weird = cp.CustomPriceSettings(day_price=2.0, short_premium=0, min_price=10, max_days=200)
        prices = [price(d, settings=weird) for d in range(1, 201)]
        self.assertEqual(prices, sorted(prices))

    def test_monotonic_without_tariffs(self):
        prices = [price(d, tariffs=[]) for d in range(1, 366)]
        self.assertEqual(prices, sorted(prices))


class FloorTests(unittest.TestCase):
    def test_minimum_check(self):
        self.assertEqual(price(1), 59)
        self.assertEqual(price(3), 59)
        self.assertEqual(cp.compute_custom_price(S, 1, TARIFFS).floor_reason, "min_price")

    def test_not_cheaper_than_shorter_tariff(self):
        # 7 дней по формуле ~55 ₽, но тариф «Неделя» стоит 74 ₽
        self.assertGreaterEqual(price(7), 74)
        self.assertGreaterEqual(price(30), 149)
        self.assertGreaterEqual(price(90), 399)

    def test_reference_values(self):
        expected = {7: 79, 14: 99, 30: 199, 90: 529}
        for days, value in expected.items():
            self.assertEqual(price(days), value, days)

    def test_every_custom_day_costs_more_than_best_tariff_day(self):
        best_rate = min(t.price / t.days for t in TARIFFS)
        for d in range(1, 366):
            self.assertGreater(price(d) / d, best_rate, d)

    def test_cheap_day_price_cannot_undercut_tariffs(self):
        dumping = cp.CustomPriceSettings(day_price=1.0, short_premium=0, min_price=1)
        for d in (10, 45, 120, 300):
            self.assertGreaterEqual(price(d, settings=dumping), d * 399 / 90 - 1e-9, d)

    def test_floor_reason_for_long_term(self):
        self.assertEqual(cp.compute_custom_price(S, 365, TARIFFS).floor_reason, "formula")


class ValidationTests(unittest.TestCase):
    def test_zero_and_negative_rejected(self):
        for d in (0, -5):
            with self.assertRaises(cp.CustomDaysError):
                price(d)

    def test_above_max_rejected(self):
        with self.assertRaises(cp.CustomDaysError):
            price(366)

    def test_max_follows_settings(self):
        self.assertEqual(price(400, settings=cp.CustomPriceSettings(max_days=400)) % 10, 9)

    def test_non_integer_rejected(self):
        for bad in (1.5, "7", True, None):
            with self.assertRaises(cp.CustomDaysError):
                cp.compute_custom_price(S, bad, TARIFFS)


class HintTests(unittest.TestCase):
    def test_hint_points_to_cheaper_standard_tariff(self):
        # 25 дней ≈ 169 ₽, а месяц стоит 149 ₽ и даёт больше
        result = cp.compute_custom_price(S, 25, TARIFFS)
        self.assertIsNotNone(result.hint)
        self.assertEqual(result.hint.name, "Месяц")
        self.assertEqual(result.hint.saving, result.price - 149)

    def test_no_hint_when_custom_is_cheaper(self):
        result = cp.compute_custom_price(S, 45, TARIFFS)  # 279 ₽ против 399 ₽ за 90 дн.
        self.assertIsNone(result.hint)

    def test_no_hint_without_tariffs(self):
        self.assertIsNone(cp.compute_custom_price(S, 10, []).hint)

    def test_breakdown_mentions_floor(self):
        text = cp.compute_custom_price(S, 1, TARIFFS).breakdown(S.day_price)
        self.assertIn("минимальный чек", text)
        self.assertIn("59", text)


class PreviewTests(unittest.TestCase):
    def test_table_respects_max_days(self):
        rows = cp.preview_table(cp.CustomPriceSettings(max_days=100), TARIFFS)
        self.assertTrue(all(r.days <= 100 for r in rows))
        self.assertEqual(rows[0].days, 1)

    def test_table_is_monotonic(self):
        prices = [r.price for r in cp.preview_table(S, TARIFFS)]
        self.assertEqual(prices, sorted(prices))


if __name__ == "__main__":
    unittest.main()
