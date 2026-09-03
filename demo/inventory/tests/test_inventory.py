"""What the shop is allowed to do.

`remove` is the interesting one. A shop cannot take out more than it has, and
it cannot take out something it never had, and both of those are ordinary
mistakes for a person at a till to make rather than crashes.
"""

from __future__ import annotations

import unittest

import inventory

SHELF = {"apples": 3, "bread": 1}


class AddTests(unittest.TestCase):
    def test_a_new_item_appears(self) -> None:
        self.assertEqual(inventory.add({}, "apples", 5)["apples"], 5)

    def test_more_of_something_adds_up(self) -> None:
        self.assertEqual(inventory.add(SHELF, "apples", 2)["apples"], 5)

    def test_a_negative_delivery_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            inventory.add(SHELF, "apples", -1)

    def test_the_shelf_it_was_given_is_left_alone(self) -> None:
        before = dict(SHELF)
        inventory.add(SHELF, "apples", 2)
        self.assertEqual(SHELF, before)


class RemoveTests(unittest.TestCase):
    def test_taking_some_out_leaves_the_rest(self) -> None:
        self.assertEqual(inventory.remove(SHELF, "apples", 2)["apples"], 1)

    def test_taking_all_of_something_out_leaves_zero(self) -> None:
        self.assertEqual(inventory.remove(SHELF, "bread", 1)["bread"], 0)

    def test_the_shelf_it_was_given_is_left_alone(self) -> None:
        before = dict(SHELF)
        inventory.remove(SHELF, "apples", 1)
        self.assertEqual(SHELF, before)

    def test_taking_out_more_than_there_is_refused(self) -> None:
        """A shelf cannot hold minus two apples."""
        with self.assertRaises(ValueError):
            inventory.remove(SHELF, "apples", 5)

    def test_stock_never_goes_negative(self) -> None:
        for count in (4, 10, 100):
            with self.assertRaises(ValueError):
                inventory.remove(SHELF, "apples", count)

    def test_taking_out_something_we_never_had_is_refused(self) -> None:
        """An ordinary mistake at a till, so it should read like one."""
        with self.assertRaises(ValueError):
            inventory.remove(SHELF, "cheese", 1)

    def test_a_negative_removal_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            inventory.remove(SHELF, "apples", -1)


class ReportTests(unittest.TestCase):
    def test_an_empty_shelf_says_so(self) -> None:
        self.assertEqual(inventory.report({}), "nothing in stock")

    def test_items_are_listed_alphabetically(self) -> None:
        lines = inventory.report({"pears": 1, "apples": 2}).splitlines()
        self.assertTrue(lines[0].startswith("apples"))

    def test_every_item_is_shown_with_its_count(self) -> None:
        text = inventory.report(SHELF)
        self.assertIn("apples", text)
        self.assertIn("3", text)


if __name__ == "__main__":
    unittest.main()
