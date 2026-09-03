# loginpage

A sign-in page, used as the second worked example for the
[yatra-harness](../../README.md). Where [tictactoe](../tictactoe) is pure
functions with no UI, this one has a screen: the point is to change something
you can look at, and then look at it.

## Run it

```
cd demo/loginpage
ay
```

No flags. `ay` works in the directory you started it in and reads the
`AGENTS.md` here on its own.

Then type this at the `>` prompt:

```
The sign-in page is broken in three ways and the tests prove it. Run the
tests to see the eight failures, then fix all three: in auth.py every failed
sign-in must return the same message so the page does not reveal which
usernames exist; in page.py both inputs need real labels and the error needs
to be announceable; in static/style.css the error colour is invisible against
the card. Read README.md and AGENTS.md first. Do not edit anything under
tests/. Run the tests again at the end.
```

Eight tests fail before, thirty-five pass after. It takes about a minute.

## Then look at it

```
python app.py
```

Open http://localhost:8000 and sign in as `ada` with the **wrong** password.

**Before the fix** the page reloads and nothing appears to change. **After**,
a readable message appears that does not say whether `ada` exists, both fields
carry visible labels, and a screen reader announces the failure.

Sign in properly with `ada` / `difference-engine`, or `grace` / `nanosecond`.
Standard library only. No build step, no framework, no network.

Put it back with `git checkout -- demo/loginpage`.

## Checking by hand

```
python -m unittest discover -s tests
```

The repository is **deliberately broken**, in three ways that look unrelated
and are not.

## 1. The page tells strangers who has an account

Type a username that does not exist and the page says *"We do not have an
account with that username."* Type a real one with the wrong password and it
says *"That password is not correct."*

Those two answers are a list of everyone who has an account, handed to anyone
who asks. It is called username enumeration, and it is the reason a real login
form gives one identical answer to every failure.

```
python -m unittest tests.test_auth
```

Four tests fail. The fix is in `auth.py`.

## 2. Nothing on the form has a name

Both inputs are labelled with `placeholder` and nothing else. A placeholder
disappears the moment you type, is styled faint by default, and is not
announced reliably by screen readers, so someone using one hears only
*"edit text"* twice and has to guess which field is which.

Missing form labels are the third most common accessibility failure on the
web, on roughly half of the top million home pages. This is that failure.

The error message has the same problem from the other direction: it is an
ordinary paragraph, so a screen reader that has already read the page never
mentions that anything went wrong.

```
python -m unittest tests.test_page
```

Three tests fail. The fix is in `page.py`.

## 3. The failure message is invisible

`--danger` is `#f4f6f8`. The card behind it is `#ffffff`. That is a contrast
ratio of **1.08:1**, where readable body text needs 4.5:1.

So the message is in the HTML, it is correct, it is sent, and the visitor sees
a blank gap. A failed sign-in looks exactly like a button that did nothing.

```
python -m unittest tests.test_style
```

One test fails. The fix is in `static/style.css`.

## Why all three

They are independent — any one can be done first — but they are the same bug
seen from three places, and that is the interesting part.

Fix the enumeration on its own and the message becomes safe, stays invisible,
and is still never announced. Fix the stylesheet on its own and you have made
a message readable whose content tells strangers which usernames are real.
Fix the labels on its own and a screen reader can now name two fields on a
form that still cannot explain why it refused you.

Only together do they produce a page that tells the visitor what happened,
tells them no more than that, and tells it to all of them.

## Seeing it

The tests are the specification and they are what the harness is judged by.
The server is for you.

```
python app.py
```

Before: sign in as `ada` with the wrong password. The page reloads and nothing
appears to change.

After: the same attempt shows a readable message that does not say whether
`ada` exists, both fields carry visible labels, and a screen reader announces
the failure.
