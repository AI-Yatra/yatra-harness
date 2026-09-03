"""Who is allowed in.

Deliberately tiny: a fixed table of accounts and one function that says yes or
no. Real password storage is out of scope for a teaching exercise, so the
hashes here are plain SHA-256 and are not a recommendation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

#: The one message a failed sign-in is allowed to produce. Which half of the
#: credentials was wrong is not the visitor's business, and telling them turns
#: the form into a way of discovering who has an account.
#:
#: It does not name either field, not even to say both were wrong together:
#: "that username and password do not match" still tells a reader the form
#: knows about usernames specifically, and the test suite holds the line at
#: naming neither.
FAILURE = "Those details do not match an account."


@dataclass(frozen=True, slots=True)
class Account:
    username: str
    password_hash: str


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


ACCOUNTS: dict[str, Account] = {
    "ada": Account("ada", hash_password("difference-engine")),
    "grace": Account("grace", hash_password("nanosecond")),
}


@dataclass(frozen=True, slots=True)
class Result:
    """The outcome of one sign-in attempt."""

    ok: bool
    #: Empty when the attempt succeeded. Otherwise what the page should show.
    message: str = ""


def authenticate(username: str, password: str) -> Result:
    """Check one username and password.

    Returns a Result rather than raising: a wrong password is an ordinary
    thing for a visitor to do, not an exceptional one.
    """
    account = ACCOUNTS.get(username.strip().lower())
    if account is None:
        return Result(False, "We do not have an account with that username.")
    if account.password_hash != hash_password(password):
        return Result(False, "That password is not correct.")
    return Result(True)
