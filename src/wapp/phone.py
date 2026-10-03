"""Phone numbers as WhatsApp writes them: country code and number, digits only."""

from __future__ import annotations

import re
from typing import Optional


def normalise(raw: str) -> Optional[str]:
    """``919618344433`` from any of the ways an Indian mobile gets written, or None.

    9618344433, 09618344433, +91 96183 44433 and 0091-9618344433 are all the
    same number.  A number already carrying another country code (12 or more
    digits not starting 91, after a + or 00) is kept as it is: WhatsApp IDs
    arriving from the webhook are always in that form already.
    """
    text = (raw or "").strip()
    digits = re.sub(r"\D", "", text)
    if digits.startswith("00"):
        digits = digits[2:]
    elif text.startswith("+") and len(digits) >= 8:
        return digits
    if len(digits) == 11 and digits[0] == "0":
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "91" + digits
    if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        return digits
    if 8 <= len(digits) <= 15 and not digits.startswith("91"):
        return digits
    return None


def display(wa_id: str) -> str:
    """+91 96183 44433 - the way the firm writes a mobile number."""
    if len(wa_id) == 12 and wa_id.startswith("91"):
        return f"+91 {wa_id[2:7]} {wa_id[7:]}"
    return f"+{wa_id}"
