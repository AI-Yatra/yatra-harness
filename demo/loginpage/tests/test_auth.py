"""Signing in, and what a failure is allowed to reveal.

The interesting tests here are the ones about the failure message. A form
that says "no such user" for one attempt and "wrong password" for another has
told an anonymous visitor which usernames exist, and that turns a login page
into a way of listing the people who have accounts. Every failure has to look
identical from the outside.
"""

from __future__ import annotations

import unittest

import auth


class SuccessTests(unittest.TestCase):
    def test_the_right_credentials_are_accepted(self) -> None:
        self.assertTrue(auth.authenticate("ada", "difference-engine").ok)

    def test_a_second_account_works_too(self) -> None:
        self.assertTrue(auth.authenticate("grace", "nanosecond").ok)

    def test_a_username_is_not_case_sensitive(self) -> None:
        self.assertTrue(auth.authenticate("Ada", "difference-engine").ok)

    def test_surrounding_space_in_a_username_is_forgiven(self) -> None:
        """Copied and pasted usernames carry it, and it is not a typo."""
        self.assertTrue(auth.authenticate("  ada  ", "difference-engine").ok)

    def test_a_successful_attempt_carries_no_message(self) -> None:
        self.assertEqual(auth.authenticate("ada", "difference-engine").message, "")


class FailureTests(unittest.TestCase):
    def test_a_wrong_password_is_refused(self) -> None:
        self.assertFalse(auth.authenticate("ada", "wrong").ok)

    def test_an_unknown_username_is_refused(self) -> None:
        self.assertFalse(auth.authenticate("nobody", "difference-engine").ok)

    def test_an_empty_password_is_refused(self) -> None:
        self.assertFalse(auth.authenticate("ada", "").ok)

    def test_a_password_is_case_sensitive(self) -> None:
        self.assertFalse(auth.authenticate("ada", "DIFFERENCE-ENGINE").ok)

    def test_a_password_that_is_a_prefix_is_refused(self) -> None:
        """A comparison that stops early would let this through."""
        self.assertFalse(auth.authenticate("ada", "difference").ok)


class EnumerationTests(unittest.TestCase):
    """Every failure has to look the same to the visitor."""

    def test_an_unknown_username_and_a_wrong_password_read_identically(self) -> None:
        unknown = auth.authenticate("nobody", "whatever")
        wrong = auth.authenticate("ada", "whatever")
        self.assertEqual(unknown.message, wrong.message)

    def test_the_failure_message_is_the_one_the_module_names(self) -> None:
        self.assertEqual(auth.authenticate("ada", "wrong").message, auth.FAILURE)

    def test_no_failure_message_names_the_username_field(self) -> None:
        """"We have no account with that username" is the whole giveaway."""
        for username, password in (("nobody", "x"), ("ada", "x"), ("", "")):
            message = auth.authenticate(username, password).message.lower()
            self.assertNotIn("username", message)
            self.assertNotIn("no account", message)

    def test_no_failure_message_singles_out_the_password(self) -> None:
        """Saying the password was wrong confirms the username was right."""
        message = auth.authenticate("ada", "wrong").message.lower()
        self.assertNotIn("password is not correct", message)
        self.assertNotIn("wrong password", message)

    def test_a_failure_still_says_something(self) -> None:
        """Identical is not the same as absent; the visitor needs telling."""
        self.assertTrue(auth.authenticate("ada", "wrong").message.strip())


if __name__ == "__main__":
    unittest.main()
