# loginpage

A sign-in page, used as the second worked example for the
[yatra-harness](../../README.md). Where [tictactoe](../tictactoe) is pure
functions with no UI, this one has a screen: the point is to change something
you can look at, and then look at it.

## Look at it first

```
cd demo/loginpage
python app.py
```

Open http://localhost:8000. **One fault you can see, three you cannot.**

### What you can see

The username and password boxes stick out past the edges of the card, about
26 pixels either side. `.control` is `width: 100%` with padding and a border,
and nothing sets `box-sizing: border-box`, so the browser adds the padding on
top of the full width. It is the most common layout bug there is.

### What you cannot see

Sign in as `ada` with the **wrong** password. The page reloads and *nothing
appears to change*.

Something did happen. The page sent back "That password is not correct." It is
in the HTML, it is correct, and it is painted `#f4f6f8` on a white card — a
contrast ratio of **1.08:1**, where readable text needs 4.5:1. A failed
sign-in looks exactly like a button that did nothing.

Now try a username that does not exist. Same blank gap, but the hidden text is
different: "We do not have an account with that username." Those two answers
are a list of everyone who has an account, handed to anyone who asks.

And start typing in either box. The word inside it disappears, because the
fields are labelled with `placeholder` and nothing else. Once you have typed,
nothing on screen says which box is which — and a screen reader never said in
the first place.

So: one fault announces itself, and three hide. That is the point of the
exercise.

## Fix it

```
cd demo/loginpage
ay
```

No flags. `ay` works in the directory you started it in and reads the
`AGENTS.md` here on its own.

Then paste this at the `>` prompt:

```
The sign-in page is broken in four ways and the tests prove it. Run
`python -m unittest discover -s tests` to see the nine failures, then fix all
four: in static/style.css the input fields overflow the card because nothing
sets box-sizing, and the error colour is invisible against the card; in
auth.py every failed sign-in must return the same message so the page does
not reveal which usernames exist; in page.py both inputs need real labels and
the error needs to be announceable. Read README.md and AGENTS.md first. Do
not edit anything under tests/. Run the tests again at the end.
```

Nine tests fail before, thirty-six pass after. It takes about a minute.

## Look again

```
python app.py
```

The fields now sit inside the card. A wrong password produces a readable
message that does not say whether `ada` exists. Both fields carry visible
labels that stay put while you type. A screen reader announces the failure.

Sign in properly with `ada` / `difference-engine`, or `grace` / `nanosecond`.

Put it back with `git checkout -- demo/loginpage`.

## What each fault is, exactly

**1. The fields overflow the card.** `static/style.css`, one test. No
`box-sizing: border-box`, so `width: 100%` plus padding is wider than the
space available.

**2. The page tells strangers who has an account.** `auth.py`, four tests. An
unknown username and a wrong password produce different messages. This is
username enumeration, and it is why a real login form gives one identical
answer to every failure.

**3. Nothing on the form has a name.** `page.py`, three tests. Both inputs are
labelled by `placeholder`, which disappears on typing and is not announced
reliably by screen readers. Missing form labels are the third most common
accessibility failure on the web, on roughly half of the top million home
pages. The error message has the same problem from the other side: it is an
ordinary paragraph, so a screen reader that has already read the page never
mentions that anything went wrong.

**4. The failure message is invisible.** `static/style.css`, one test.
`--danger` is `#f4f6f8` against a `#ffffff` card: 1.08:1, where body text
needs 4.5:1.

## Why all four

They are independent — any one can be done first — and three of them are the
same bug seen from different sides, which is the interesting part.

Fix the enumeration on its own and the message becomes safe, stays invisible,
and is still never announced. Fix the stylesheet on its own and you have made
a message readable whose content tells strangers which usernames are real.
Fix the labels on its own and a screen reader can now name two fields on a
form that still cannot explain why it refused you.

Only together do they produce a page that tells the visitor what happened,
tells them no more than that, and tells it to all of them.

The overflowing fields are the odd one out, and deliberately so. Without a
fault you can see, this page looks finished and behaves as though nothing is
wrong — which is exactly how a page with the other three ships.
