"""Hidden characters: the one predicate and the one escape function for printed text.

Every text nl-shell prints from a proposal, from ``:context`` or from a start-up error goes
through ``escape`` (threat model, "Output rendering is filtered everywhere"), and the
proposal parser (``nl_shell.proposal``) rejects a ``command`` holding any character
``is_hidden`` flags ("What the user confirms is exactly what runs"). Both use the same
predicate, so the character policy lives here and nowhere else (#144 refines it).

Hidden here means: C0 controls except LF and TAB, DEL, C1 controls, the bidi controls,
every Unicode format character (category Cf), U+2028 and U+2029, the tag block
U+E0000-U+E007F, and lone surrogates.

``escape`` writes a hidden character as ``\\xHH`` (up to U+00FF), ``\\uHHHH`` (up to
U+FFFF) or ``\\UHHHHHHHH``, in lowercase hex; everything else is unchanged, LF and TAB
included. With ``backslash=True`` (``:context``) a backslash is written as ``\\\\`` too,
so a real ESC (``\\x1b``) and the typed text ``\\x1b`` (``\\\\x1b``) print differently.
"""

from __future__ import annotations

import unicodedata

# The bidi controls: ALM, LRM, RLM, LRE..RLO, LRI..PDI. All are Cf today; listed so the
# set does not depend on the Unicode version Python ships.
BIDI_CONTROLS = frozenset(
    (0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A))
)
LINE_AND_PARAGRAPH_SEPARATORS = frozenset((0x2028, 0x2029))
TAG_BLOCK = range(0xE0000, 0xE0080)
SURROGATES = range(0xD800, 0xE000)
VISIBLE_CONTROLS = frozenset("\n\t")


def is_hidden(char: str) -> bool:
    """True when ``char`` (one code point) must not reach the screen as it is."""
    if char in VISIBLE_CONTROLS:
        return False
    code = ord(char)
    return (
        code < 0x20
        or 0x7F <= code <= 0x9F
        or code in BIDI_CONTROLS
        or code in LINE_AND_PARAGRAPH_SEPARATORS
        or code in TAG_BLOCK
        or code in SURROGATES
        or unicodedata.category(char) == "Cf"
    )


def has_hidden(text: str) -> bool:
    """True when any character of ``text`` is hidden."""
    return any(is_hidden(char) for char in text)


def escape_char(char: str) -> str:
    code = ord(char)
    if code <= 0xFF:
        return f"\\x{code:02x}"
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def escape(text: str, *, backslash: bool = False) -> str:
    """``text`` with every hidden character escaped (and backslashes, if asked)."""
    out = []
    for char in text:
        if backslash and char == "\\":
            out.append("\\\\")
        elif is_hidden(char):
            out.append(escape_char(char))
        else:
            out.append(char)
    return "".join(out)
