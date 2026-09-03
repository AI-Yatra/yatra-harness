#!/bin/sh
# Install `ay` and `harness` on macOS or Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/AI-Yatra/yatra-harness/main/install.sh | sh
#
# What this does, in order: refuses to run under sudo, finds a downloader,
# installs uv if the machine has none, installs this package as a uv tool, and
# then runs `ay` to prove the result actually works before saying it does.
#
# uv is the whole trick. It ships its own CPython for macOS, Linux and Windows
# and downloads one automatically, so nothing here needs Python to already be
# present -- which is the entire difficulty of shipping a Python CLI.
#
# POSIX sh on purpose: this has to run under sh, dash, bash and zsh, so there
# are no arrays, no `local`, and no `pipefail`.

set -eu

REPO="AI-Yatra/yatra-harness"
PACKAGE="yatra-harness"
# Not optional. The REPL spawns the harness with its own interpreter, and the
# workshop's spreadsheet task needs openpyxl available there.
EXTRA="openpyxl"

AY_REF="${AY_REF:-main}"
AY_PYTHON="${AY_PYTHON:-3.12}"
AY_SOURCE="${AY_SOURCE:-}"
AY_ALLOW_SUDO="${AY_ALLOW_SUDO:-0}"
AY_DRY_RUN="${AY_DRY_RUN:-0}"

BOLD=""
DIM=""
RESET=""
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD="$(printf '\033[1m')"
    DIM="$(printf '\033[2m')"
    RESET="$(printf '\033[0m')"
fi

say() { printf '%s\n' "  $*"; }
step() { printf '%s\n' "${BOLD}==>${RESET} $*"; }
die() {
    printf '%s\n' "" >&2
    printf '%s\n' "error: $*" >&2
    exit 1
}

usage() {
    cat <<EOF
Install ay, the AI-Yatra harness.

  curl -fsSL https://raw.githubusercontent.com/$REPO/main/install.sh | sh

Options (as environment variables, since a piped script takes no arguments):

  AY_REF=main          branch or tag to install from
  AY_PYTHON=3.12       Python version for the tool environment
  AY_SOURCE=...        install this instead: a PyPI name, a URL, or a path
  AY_ALLOW_SUDO=1      permit running under sudo
  AY_DRY_RUN=1         print what would happen and stop
  UV_TOOL_BIN_DIR=...  where the ay and harness commands are placed
  NO_COLOR=1           plain output

Read before running:

  curl -fsSL https://raw.githubusercontent.com/$REPO/main/install.sh -o install.sh
  less install.sh
  sh install.sh
EOF
}

for argument in "$@"; do
    case "$argument" in
        -h | --help) usage; exit 0 ;;
        --dry-run) AY_DRY_RUN=1 ;;
        *) die "unknown option: $argument. Run with --help." ;;
    esac
done

# ---------------------------------------------------------------- guard rails

# Installing under sudo puts everything in root's home and leaves files the
# operator cannot write. Claude Code's installer refuses this too.
if [ "${AY_ALLOW_SUDO}" != "1" ] && [ -n "${SUDO_USER:-}" ]; then
    die "do not run this under sudo. It installs into your own home directory.
       Run it as yourself, or set AY_ALLOW_SUDO=1 if you are certain."
fi

DOWNLOAD=""
if command -v curl >/dev/null 2>&1; then
    DOWNLOAD="curl"
elif command -v wget >/dev/null 2>&1; then
    DOWNLOAD="wget"
else
    die "neither curl nor wget is available, and one of them is needed."
fi

fetch_to_stdout() {
    if [ "$DOWNLOAD" = "curl" ]; then
        curl -fsSL "$1"
    else
        wget -qO- "$1"
    fi
}

# True when the URL answers. Used to tell a published package from one that is
# not on PyPI yet, so the same script works before and after publishing.
url_exists() {
    if [ "$DOWNLOAD" = "curl" ]; then
        curl -fsSL -o /dev/null "$1" 2>/dev/null
    else
        wget -q --spider "$1" 2>/dev/null
    fi
}

# ------------------------------------------------------------------------ uv

printf '%s\n' ""
step "Looking for uv"
UV=""
if command -v uv >/dev/null 2>&1; then
    UV="$(command -v uv)"
    say "found $($UV --version 2>/dev/null || echo uv)"
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
    say "found $("$UV" --version 2>/dev/null || echo uv) at $UV"
else
    say "not installed, fetching it from astral.sh"
    if [ "$AY_DRY_RUN" = "1" ]; then
        say "${DIM}(dry run: skipping)${RESET}"
    else
        fetch_to_stdout "https://astral.sh/uv/install.sh" | sh >/dev/null 2>&1 \
            || die "could not install uv. Install it yourself from https://docs.astral.sh/uv/
       and run this script again."
        if [ -x "$HOME/.local/bin/uv" ]; then
            UV="$HOME/.local/bin/uv"
        elif command -v uv >/dev/null 2>&1; then
            UV="$(command -v uv)"
        else
            die "uv installed but could not be found afterwards."
        fi
        say "installed $("$UV" --version 2>/dev/null || echo uv)"
    fi
fi
[ -n "$UV" ] || UV="uv"

# --------------------------------------------------------------------- source

step "Choosing a source"
if [ -n "$AY_SOURCE" ]; then
    SOURCE="$AY_SOURCE"
    say "$SOURCE ${DIM}(from AY_SOURCE)${RESET}"
elif url_exists "https://pypi.org/simple/$PACKAGE/"; then
    SOURCE="$PACKAGE"
    say "PyPI: $PACKAGE"
else
    # Not on PyPI yet. A source tarball needs no git binary, which one more
    # prerequisite is one more thing to go wrong.
    SOURCE="https://github.com/$REPO/archive/refs/heads/$AY_REF.tar.gz"
    say "GitHub: $REPO@$AY_REF ${DIM}(not on PyPI yet)${RESET}"
fi

# -------------------------------------------------------------------- install

step "Installing"
say "python $AY_PYTHON, with $EXTRA"
if [ "$AY_DRY_RUN" = "1" ]; then
    printf '%s\n' ""
    say "${DIM}dry run, nothing was changed. Would have run:${RESET}"
    say "$UV tool install --force --python $AY_PYTHON --with $EXTRA $SOURCE"
    printf '%s\n' ""
    exit 0
fi

"$UV" tool install --force --python "$AY_PYTHON" --with "$EXTRA" "$SOURCE" >/dev/null 2>&1 \
    || die "the install failed. To see why, run it without the quiet flag:
       $UV tool install --force --python $AY_PYTHON --with $EXTRA $SOURCE"

BIN="$("$UV" tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
AY="$BIN/ay"
[ -x "$AY" ] || die "installed, but $AY is missing. This is a packaging fault, not your machine."

# ---------------------------------------------------------------- verification

# The step that matters. An earlier version of this package installed cleanly
# and then failed on first run, because its default config was not inside the
# wheel. Anything that only checks "did the install command succeed" would have
# reported success. Starting the REPL and exiting loads the config, resolves a
# route and builds the prompt.
#
# `--model local` because the machine this runs on has no API key yet, and the
# default route wants one. Asking for a credential is `ay` behaving correctly,
# not a broken install, and the two must not look the same here. The local
# route needs no key and no server to start and exit, while still reading the
# config that was the thing missing.
step "Checking it runs"
if printf '/exit\n' | "$AY" --model local >/dev/null 2>&1; then
    say "ay starts and loads its config"
else
    printf '%s\n' "" >&2
    printf '%s\n' "  ay was installed but does not start. Output:" >&2
    printf '/exit\n' | "$AY" --model local 2>&1 | sed 's/^/  /' >&2 || true
    die "install incomplete."
fi

# ---------------------------------------------------------------------- finish

printf '%s\n' ""
step "Done"
say "ay        $AY"
say "harness   $BIN/harness"

case ":${PATH}:" in
    *":${BIN}:"*)
        printf '%s\n' ""
        say "Run ${BOLD}ay${RESET} in any directory to start."
        ;;
    *)
        printf '%s\n' ""
        say "${BOLD}$BIN is not on your PATH yet.${RESET}"
        say "Add it for this shell:"
        say "  ${DIM}export PATH=\"$BIN:\$PATH\"${RESET}"
        say "Or permanently, for every new shell:"
        say "  ${DIM}$UV tool update-shell${RESET}"
        ;;
esac
printf '%s\n' ""
say "${DIM}First run needs no API key: ay --model local, or see the README.${RESET}"
printf '%s\n' ""
