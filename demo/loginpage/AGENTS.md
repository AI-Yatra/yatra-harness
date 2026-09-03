# AGENTS.md

A sign-in page served by the standard library. No framework, no build step, no
network, no dependencies. The tests are the specification.

## Stack

Python 3.11+, standard library only. Tests are `unittest`, not pytest. The
markup is a formatted string in `page.py`; the stylesheet is plain CSS that
the browser gets verbatim.

## Verification

```
python -m unittest discover -s tests
```

Run it from the repository root; the tests import `auth` and `page` from
there.

`python app.py` serves the page on http://localhost:8000 for a person to look
at. **Do not start it as a verification step** — it does not exit, so it will
hang the run. The tests are what decides whether the work is done.

## Hard constraints

- **Do not edit anything under `tests/`.** The tests are the specification.
  Changing one to make it pass changes what the page is supposed to be, which
  is not a fix.
- **Every failed sign-in produces the same message**, whatever was wrong with
  it. A different answer for an unknown username than for a bad password tells
  an anonymous visitor which usernames exist.
- **Do not weaken the tests' thresholds.** 4.5:1 is the WCAG contrast ratio for
  body text, not a preference.
- Keep it dependency-free and keep the markup escaped: `page.render` is given
  a username the visitor typed.
- Passwords are compared as SHA-256 hashes. That is not a recommendation for
  real software; it keeps the exercise about the page.

## Layout

| Path | What it holds |
|---|---|
| `auth.py` | the account table and the sign-in decision |
| `page.py` | the whole document, as HTML |
| `static/style.css` | every colour and every rule the browser sees |
| `app.py` | the server, so a person can look at the page |
| `tests/test_auth.py` | who gets in, and what a failure may reveal |
| `tests/test_page.py` | the markup, read as a browser and a screen reader read it |
| `tests/test_style.py` | colour, measured as contrast ratios rather than eyeballed |
