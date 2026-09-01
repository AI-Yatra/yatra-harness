"""The rules of the game."""

from __future__ import annotations

import unittest

from game import EMPTY, is_draw, is_full, new_board, place, winner


def board_from(text: str) -> list[str]:
    """Build a board from nine characters, using '.' for an empty cell."""
    cells = [character for character in text if character in "XO."]
    if len(cells) != 9:
        raise ValueError("a board needs exactly nine cells")
    return [EMPTY if cell == "." else cell for cell in cells]


class PlacementTests(unittest.TestCase):
    def test_a_new_board_is_empty(self) -> None:
        self.assertEqual(new_board(), [EMPTY] * 9)

    def test_placing_marks_the_cell(self) -> None:
        self.assertEqual(place(new_board(), 4, "X")[4], "X")

    def test_placing_does_not_change_the_original_board(self) -> None:
        board = new_board()
        place(board, 0, "X")
        self.assertEqual(board, new_board())

    def test_a_taken_cell_is_refused(self) -> None:
        board = place(new_board(), 0, "X")
        with self.assertRaises(ValueError):
            place(board, 0, "O")

    def test_an_unknown_player_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            place(new_board(), 0, "Z")

    def test_a_cell_outside_the_board_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            place(new_board(), 9, "X")


class WinnerTests(unittest.TestCase):
    def test_an_empty_board_has_no_winner(self) -> None:
        self.assertIsNone(winner(new_board()))

    def test_a_full_row_wins(self) -> None:
        self.assertEqual(winner(board_from("XXX" "OO." "...")), "X")

    def test_a_middle_row_wins(self) -> None:
        self.assertEqual(winner(board_from("..." "OOO" "XX.")), "O")

    def test_a_full_column_wins(self) -> None:
        self.assertEqual(winner(board_from("X.O" "X.O" "X..")), "X")

    def test_a_last_column_wins(self) -> None:
        self.assertEqual(winner(board_from("X.O" "X.O" ".XO")), "O")

    def test_the_leading_diagonal_wins(self) -> None:
        self.assertEqual(winner(board_from("XO." ".XO" "..X")), "X")

    def test_the_other_diagonal_wins(self) -> None:
        # Top-right to bottom-left. This is a winning line like any other.
        self.assertEqual(winner(board_from(".OX" ".XO" "X..")), "X")

    def test_a_mixed_line_does_not_win(self) -> None:
        self.assertIsNone(winner(board_from("XOX" "OXO" "OXO")))


class BoardStateTests(unittest.TestCase):
    def test_a_full_board_is_full(self) -> None:
        self.assertTrue(is_full(board_from("XOX" "XOX" "OXO")))

    def test_a_board_with_a_gap_is_not_full(self) -> None:
        self.assertFalse(is_full(board_from("XOX" "XO." "OXO")))

    def test_a_full_board_with_no_winner_is_a_draw(self) -> None:
        self.assertTrue(is_draw(board_from("XOX" "XOO" "OXX")))

    def test_a_won_board_is_not_a_draw(self) -> None:
        self.assertFalse(is_draw(board_from("XXX" "OO." "...")))


if __name__ == "__main__":
    unittest.main()
