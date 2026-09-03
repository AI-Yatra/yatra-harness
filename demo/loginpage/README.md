# loginpage

A sign-in page, used as the second worked example for the
[yatra-harness](../../README.md). Where [tictactoe](../tictactoe) is pure
functions with no UI, this one has a screen: the point is to change something
you can look at, and then look at it.

```
python -m unittest discover -s tests     # what the harness judges
python app.py                            # then open http://localhost:8000
```

Sign in as `ada` with `difference-engine`, or `grace` with `nanosecond`.
Standard library only. No build step, no framework, no network.

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
