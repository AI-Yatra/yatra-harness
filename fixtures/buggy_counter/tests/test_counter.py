import unittest

from counter import clamp


class ClampTests(unittest.TestCase):
    def test_value_inside_interval_is_unchanged(self) -> None:
        self.assertEqual(clamp(15, 10, 20), 15)

    def test_value_above_interval_uses_upper_bound(self) -> None:
        self.assertEqual(clamp(25, 10, 20), 20)

    def test_value_below_interval_uses_configured_lower_bound(self) -> None:
        self.assertEqual(clamp(5, 10, 20), 10)

    def test_negative_interval_uses_its_lower_bound(self) -> None:
        self.assertEqual(clamp(-10, -5, 5), -5)

    def test_reversed_interval_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "lower must not exceed upper"):
            clamp(10, 20, 10)


if __name__ == "__main__":
    unittest.main()

