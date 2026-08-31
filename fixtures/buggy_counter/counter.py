"""Small workshop fixture containing one deliberate boundary bug."""


def clamp(value: int, lower: int, upper: int) -> int:
    """Return *value* constrained to the inclusive [lower, upper] interval."""
    if lower > upper:
        raise ValueError("lower must not exceed upper")
    if value > upper:
        return upper
    return value

