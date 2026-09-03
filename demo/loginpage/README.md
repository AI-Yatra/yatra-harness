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

Open http://localhost:8000. **The page is visibly a mess before you touch
anything.**

### 1. The boxes do not fit the card

Both input fields punch out past the card's edges, thirteen pixels either
side. `.control` is `width: 100%` with padding and a border, and nothing sets
`box-sizing: border-box`, so the browser adds the padding on top of the full
width. The most common layout bug there is.

### 2. Neither box says what it is

Two identical blank rectangles. No label above them, no grey word inside
them, nothing. You have to guess that the first one is the username, and a
screen reader announces only "edit text" — twice.

### 3. The button barely says "Sign in"

The label is `#4d94d6` on a `#0069c2` button: **1.72:1**, where readable text
needs 4.5:1. You can tell there is a word there and very little else.

### 4. Failing produces an empty red box

Sign in as `ada` with the **wrong** password. A red-bordered alert panel
appears with a pink wash — and it is **empty**.

It is not empty. It says "That password is not correct.", painted `#f4f6f8`
on a `#fdecec` background: **1.05:1**. The page is shouting that something
went wrong and refusing to say what, which is worse than saying nothing.

### 5. And what it is hiding is a leak

Now try a username that does not exist. The same empty red box, but the
invisible text is different: "We do not have an account with that username."

Those two answers are a list of everyone who has an account, handed to anyone
who asks. It is called username enumeration, and it is the one fault here that
stays hidden even after you can read the message — which is exactly why the
other four have to be fixed before you can see it.

## Fix it

```
cd demo/loginpage
ay
```

No flags. `ay` works in the directory you started it in and reads the
`AGENTS.md` here on its own.

Then paste this at the `>` prompt:

```
The sign-in page is a mess and the tests prove it. Run
`python -m unittest discover -s tests` to see the eleven failures, then fix
all five. In static/style.css: the fields overflow the card because nothing
sets box-sizing, the error text is invisible against its own panel, and the
button label is unreadable on the button. In page.py: both inputs need real
labels, and the error needs to be announceable by a screen reader. In
auth.py: every failed sign-in must return the same message, so the page does
not reveal which usernames exist. Read README.md and AGENTS.md first. Do not
edit anything under tests/. Run the tests again at the end.
```

Eleven tests fail before, thirty-eight pass after. It takes about a minute.

## Look again

```
python app.py
```

The fields sit inside the card. Both are labelled, and the labels stay put
while you type. The button says "Sign in" clearly. A wrong password produces a
readable message in the red panel — and that message no longer tells you
whether `ada` exists.

Sign in properly with `ada` / `difference-engine`, or `grace` / `nanosecond`.

Put it back with `git checkout -- demo/loginpage`.

## What each fault is, exactly

| # | fault | file | tests |
|---|---|---|---|
| 1 | fields overflow the card | `static/style.css` | 1 |
| 2 | fields have no name at all | `page.py` | 4 |
| 3 | button label unreadable | `static/style.css` | 1 |
| 4 | error text invisible on its own panel | `static/style.css` | 2 |
| 5 | failure message reveals which usernames exist | `auth.py` | 4 |

**1. The fields overflow the card.** No `box-sizing: border-box`, so
`width: 100%` plus padding and a border is wider than the space available.

**2. The fields have no name at all.** No `<label>`, and no placeholder
either. A sighted visitor guesses; a screen reader announces "edit text"
twice. Missing form labels are the third most common accessibility failure on
the web, on roughly half of the top million home pages. The error region has
the matching problem: it is an ordinary paragraph rather than an alert, so a
screen reader that has already read the page never mentions the failure.

**3. The button label is unreadable.** `#4d94d6` on `#0069c2` is 1.72:1,
where body text needs 4.5:1.

**4. The error text is invisible on its own panel.** `#f4f6f8` on a `#fdecec`
wash is 1.05:1. The panel is bordered and tinted, so the page clearly
announces that something went wrong and then refuses to say what. An empty
alert is worse than no alert.

**5. The failure message reveals which usernames exist.** An unknown username
and a wrong password produce different text. This is username enumeration, and
it is why a real login form gives one identical answer to every failure.

## Why all five

Four of these you can see; the fifth you cannot, and that is the point.

The layout, the anonymous fields, the unreadable button and the empty alert
all announce themselves the moment you open the page. The leak does not. It
sits inside a message nobody can read, and it stays invisible until you have
fixed the contrast — at which point it becomes the obvious problem, in plain
English, on screen.

So the order matters. Fix the stylesheet alone and you have made readable a
message that tells strangers which usernames are real. Fix `auth.py` alone and
the message becomes safe, stays invisible, and is still never announced. Only
together do they produce a page that tells the visitor what happened, tells
them no more than that, and tells it to all of them.
