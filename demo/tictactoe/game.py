"""Noughts and crosses on a 3x3 board.

A board is a list of nine cells, read left to right and top to bottom:

    0 1 2
    3 4 5
    6 7 8

Each cell holds "X", "O" or EMPTY.
"""

from __future__ import annotations

EMPTY = " "
PLAYERS = ("X", "O")


def new_board() -> list[str]:
    """An empty board."""
    return [EMPTY] * 9


def render(board: list[str]) -> str:
    """The board as three lines, for printing."""
    rows = ["|".join(board[index : index + 3]) for index in range(0, 9, 3)]
    return "\n".join(rows)


def place(board: list[str], cell: int, player: str) -> list[str]:
    """Return a new board with `player` played at `cell`."""
    if player not in PLAYERS:
        raise ValueError(f"unknown player: {player!r}")
    if not 0 <= cell < 9:
        raise ValueError(f"cell out of range: {cell}")
    if board[cell] != EMPTY:
        raise ValueError(f"cell {cell} is already taken")
    played = list(board)
    played[cell] = player
    return played


def empty_cells(board: list[str]) -> list[int]:
    """Every cell still free, in board order."""
    return [cell for cell, value in enumerate(board) if value == EMPTY]


def is_full(board: list[str]) -> bool:
    """True when no cell is free."""
    return not empty_cells(board)


def winning_lines() -> list[tuple[int, int, int]]:
    """Every line that wins the game."""
    lines: list[tuple[int, int, int]] = []
    for row in range(3):
        start = row * 3
        lines.append((start, start + 1, start + 2))
    for column in range(3):
        lines.append((column, column + 3, column + 6))
    lines.append((0, 4, 8))
    return lines


def winner(board: list[str]) -> str | None:
    """The player occupying a whole line, or None."""
    for a, b, c in winning_lines():
        if board[a] != EMPTY and board[a] == board[b] == board[c]:
            return board[a]
    return None


def is_draw(board: list[str]) -> bool:
    """True when the board is full and nobody has won."""
    return is_full(board) and winner(board) is None
