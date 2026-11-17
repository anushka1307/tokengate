"""A cheap, deliberately pessimistic input-token estimate (docs/DECISIONS.md, D47).

The budget reserves this before the model call, and settling replaces it with the provider's
real count afterwards. An estimate below the real count lets a tenant spend more than it
reserved (measured in step 2.5: +49% with a prompt of one-letter words), so each kind of text is
counted at or above what common tokenizers charge for it:

- runs of ASCII letters/digits: 1 token per 4 characters, rounded up per run (so "a" costs 1).
  Computed as ``floor((letters + 3 * runs) / 4)``: per run ``ceil(L / 4) == floor((L + 3) / 4)``,
  and a floor of a sum is never below the sum of floors, so this is never below the per-run sum.
- an ASCII symbol or punctuation mark: 1 token each
- non-ASCII text: 2 tokens per 3 UTF-8 bytes, rounded up (an accented letter ~1.3, a CJK
  character 2, an emoji ~2.7)
- whitespace: free
- every message: +4 for the role and formatting that chat APIs add

No tokenizer and no Python-level loop over characters: a handful of ``bytes.translate`` and
``bytes.count`` calls, so it stays well under a millisecond even at the largest allowed input
and can run inline on the event loop (timed in tests/test_budget.py).
"""

import string
from collections.abc import Iterable

PER_MESSAGE_OVERHEAD = 4
ASCII_CHARS_PER_TOKEN = 4

_ALNUM = (string.ascii_letters + string.digits).encode()
_SYMBOLS = string.punctuation.encode()
_NON_ASCII = bytes(range(0x80, 0x100))
# Letters and digits become b"a", everything else b" ", so runs can be counted with bytes.count.
_RUNS = bytes(ord("a") if b in _ALNUM else ord(" ") for b in range(256))


def estimate_text_tokens(text: str) -> int:
    raw = text.encode("utf-8")
    shape = raw.translate(_RUNS)
    letters = shape.count(b"a")
    runs = shape.count(b" a") + shape.startswith(b"a")
    symbols = len(raw) - len(raw.translate(None, _SYMBOLS))
    non_ascii_bytes = len(raw) - len(raw.translate(None, _NON_ASCII))
    return (letters + 3 * runs) // ASCII_CHARS_PER_TOKEN + symbols + -(-(2 * non_ascii_bytes) // 3)


def estimate_input_tokens(messages: Iterable[str]) -> int:
    """Estimate for a chat request, given each message's text. Never below 1."""
    total = sum(PER_MESSAGE_OVERHEAD + estimate_text_tokens(text) for text in messages)
    return max(1, total)
