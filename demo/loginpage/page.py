"""The sign-in page, as HTML.

One function that returns a whole document. No template engine, because the
page is small and a dependency would be more machinery than the exercise
needs.
"""

from __future__ import annotations

from html import escape

TITLE = "Sign in to Yatra"


def render(*, error: str = "", username: str = "") -> str:
    """The sign-in page.

    `error` is shown to the visitor when a sign-in attempt failed. `username`
    is echoed back so a mistyped password does not cost them the whole form.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(TITLE)}</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<main class="card">
  <h1>Sign in</h1>
  <p class="hint">Try <code>ada</code> with <code>difference-engine</code>.</p>

  <form method="post" action="/login" class="signin" novalidate>
    <div class="field">
      <input
        class="control"
        id="username"
        name="username"
        type="text"
        placeholder="Username"
        value="{escape(username)}"
        autocomplete="username"
        autofocus>
    </div>

    <div class="field">
      <input
        class="control"
        id="password"
        name="password"
        type="password"
        placeholder="Password"
        autocomplete="current-password">
    </div>

    <p class="error">{escape(error)}</p>

    <button class="submit" type="submit">Sign in</button>
  </form>
</main>
</body>
</html>
"""


def render_welcome(username: str) -> str:
    """What a visitor sees once they are in."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(TITLE)}</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<main class="card">
  <h1>Signed in</h1>
  <p class="hint">Welcome back, {escape(username)}.</p>
  <p><a href="/">Sign out</a></p>
</main>
</body>
</html>
"""
