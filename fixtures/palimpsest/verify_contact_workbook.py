#!/usr/bin/env python3
"""Synthetic acceptance gate for the Palimpsest task.

Checks that the agent produced contact.xlsx containing every contact card
EXCEPT Tom, sorted alphabetically by full name, with columns name/email/phone.
This script never sends anything.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CARDS_DIR = ROOT / "contact_cards"
WORKBOOK = ROOT / "contact.xlsx"
EXCLUDED_NAME = "Tom"


def parse_vcards(directory: Path) -> list[dict[str, str]]:
    cards = []
    for vcf in sorted(directory.glob("*.vcf")):
        text = vcf.read_text(encoding="utf-8")
        name = ""
        email = ""
        phone = ""
        for line in text.splitlines():
            if line.startswith("FN:"):
                name = line[3:].strip()
            elif line.startswith("EMAIL"):
                email = line.split(":", 1)[1].strip()
            elif line.startswith("TEL"):
                phone = line.split(":", 1)[1].strip()
        cards.append({"name": name, "email": email, "phone": phone, "file": vcf.name})
    return cards


def is_tom(card: dict[str, str]) -> bool:
    """Exclude Tom: full name exactly 'Tom' or any name whose first token is 'Tom'."""
    name = card["name"].strip()
    return name == EXCLUDED_NAME or name.split()[0] == EXCLUDED_NAME


def main() -> int:
    all_cards = parse_vcards(CARDS_DIR)
    expected = sorted(
        [c for c in all_cards if not is_tom(c)],
        key=lambda c: c["name"].lower(),
    )
    expected_names = [c["name"] for c in expected]

    if not WORKBOOK.exists():
        print(f"FAIL: {WORKBOOK.name} does not exist")
        return 1

    try:
        import openpyxl  # type: ignore
    except ImportError:
        print("FAIL: openpyxl not available in verifier environment")
        return 1

    wb = openpyxl.load_workbook(WORKBOOK)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    header = [str(cell).strip() if cell is not None else "" for cell in rows[0]]
    if header != ["name", "email", "phone"]:
        print(f"FAIL: header is {header!r}, expected ['name', 'email', 'phone']")
        return 1

    actual_names = []
    for row in rows[1:]:
        if row[0] is None or str(row[0]).strip() == "":
            continue
        actual_names.append(str(row[0]).strip())

    if len(actual_names) != len(expected_names):
        print(
            f"FAIL: workbook has {len(actual_names)} rows, expected {len(expected_names)}"
        )
        print(f"  actual:   {actual_names}")
        print(f"  expected: {expected_names}")
        return 1

    if actual_names != expected_names:
        print(f"FAIL: names not sorted / wrong set")
        print(f"  actual:   {actual_names}")
        print(f"  expected: {expected_names}")
        return 1

    # Ensure Tom truly excluded.
    if any(is_tom({"name": n}) for n in actual_names):
        print(f"FAIL: {EXCLUDED_NAME!r} was not excluded")
        return 1

    print("PASS: contact.xlsx contains all non-Tom cards, sorted alphabetically")
    print(f"  names: {actual_names}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
