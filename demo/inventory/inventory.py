"""Stock levels for a very small shop.

The counting lives in pure functions over a plain dict, so the tests never
touch the disk and `data/stock.json` is only ever the starting numbers. The
command line at the bottom is the part that reads and writes the file.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_STOCK = Path(__file__).resolve().parent / "data" / "stock.json"

Stock = dict[str, int]


def load(path: Path = DEFAULT_STOCK) -> Stock:
    """Read the stock levels. A missing file is an empty shop, not an error."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return {str(item): int(count) for item, count in raw.items()}


def save(stock: Stock, path: Path = DEFAULT_STOCK) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stock, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def add(stock: Stock, item: str, count: int) -> Stock:
    """Take *count* more of *item* in.

    Returns a new dict; the one it was given is left alone, so a caller can
    keep the old numbers to compare against.
    """
    if count <= 0:
        raise ValueError("count must be a positive number")
    updated = dict(stock)
    updated[item] = updated.get(item, 0) + count
    return updated


def remove(stock: Stock, item: str, count: int) -> Stock:
    """Take *count* of *item* out."""
    updated = dict(stock)
    updated[item] = updated[item] - count
    return updated


def report(stock: Stock) -> str:
    """The shelf, one line per item, in alphabetical order."""
    if not stock:
        return "nothing in stock"
    width = max(len(item) for item in stock)
    return "\n".join(f"{item.ljust(width)}  {stock[item]}" for item in sorted(stock))


def main() -> int:
    parser = argparse.ArgumentParser(description="Stock levels for a very small shop.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("add", "take stock in"), ("remove", "take stock out")):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("item")
        sub.add_argument("count", type=int)
    commands.add_parser("report", help="show the shelf")

    arguments = parser.parse_args()
    stock = load()
    if arguments.command == "report":
        print(report(stock))
        return 0
    action = add if arguments.command == "add" else remove
    try:
        save(action(stock, arguments.item, arguments.count))
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}")
        return 1
    print(report(load()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
