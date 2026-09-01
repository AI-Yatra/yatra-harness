"""The move chooser.

`best_move` does not exist yet. These tests describe what it has to do, and
they are the acceptance criteria for adding it.

The rule is deliberately small and completely specified, in this order:

  1. If a cell wins the game for `player`, take it.
  2. Otherwise, if a cell would win the game for the opponent, take it.
  3. Otherwise, take the first free cell in board order.
"""

from __future__ import annotations

import unittest

from game import EMPTY, new_board

try:
    from game import best_move
except ImportError:  # pragma: no cover - the point of this module
    best_move = None


def board_from(text: str) -> list[str]:
    cells = [character for character in text if character in "XO."]
    if len(cells) != 9:
        raise ValueError("a board needs exactly nine cells")
    return [EMPTY if cell == "." else cell for cell in cells]


class BestMoveTests(unittest.TestCase):
    def setUp(self) -> None:
        if best_move is None:
            self.fail("game.best_move is not implemented yet")

    def test_a_winning_move_is_taken(self) -> None:
        # X completes the top row at cell 2.
        self.assertEqual(best_move(board_from("XX." "OO." "..."), "X"), 2)

    def test_winning_beats_blocking(self) -> None:
        # X can win at 2 and could also block O at 5. Winning comes first.
        self.assertEqual(best_move(board_from("XX." "OO." "..."), "X"), 2)

    def test_an_opponent_win_is_blocked(self) -> None:
        # X cannot win; O would complete the middle row at cell 5.
        self.assertEqual(best_move(board_from("X.." "OO." "..X"), "X"), 5)

    def test_a_column_threat_is_blocked(self) -> None:
        # O holds 1 and 4 and wins at 7. X holds 0 and 8 with no line of its
        # own, so it has nothing better to do than block.
        self.assertEqual(best_move(board_from("XO." ".O." "..X"), "X"), 7)

    def test_a_diagonal_threat_is_blocked(self) -> None:
        self.assertEqual(best_move(board_from("O.X" ".O." "X.."), "X"), 8)

    def test_otherwise_the_first_free_cell_is_taken(self) -> None:
        self.assertEqual(best_move(board_from("XO." "..." "..."), "X"), 2)

    def test_the_first_free_cell_on_an_empty_board_is_zero(self) -> None:
        self.assertEqual(best_move(new_board(), "X"), 0)

    def test_the_chosen_cell_is_always_free(self) -> None:
        board = board_from("XOX" "O.." "...")
        self.assertEqual(board[best_move(board, "X")], EMPTY)

    def test_it_works_for_either_player(self) -> None:
        self.assertEqual(best_move(board_from("OO." "XX." "..."), "O"), 2)

    def test_a_full_board_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            best_move(board_from("XOX" "XOO" "OXX"), "X")

    def test_an_unknown_player_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            best_move(new_board(), "Z")

    def test_the_board_is_not_modified(self) -> None:
        board = board_from("XX." "OO." "...")
        before = list(board)
        best_move(board, "X")
        self.assertEqual(board, before)


if __name__ == "__main__":
    unittest.main()
