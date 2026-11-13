"""API key format, generation and fingerprinting (docs/DECISIONS.md, D32 and D34).

    tg_<prefix: 12 chars>_<secret: 43 chars>       (characters: A-Z a-z 0-9)

- The prefix is public: it finds the key's row and may appear in logs and listings.
- The secret is shown once at creation and never stored. 43 characters from a 62-character
  alphabet is 43 x log2(62) ~ 256 bits of randomness.
- The database keeps HMAC-SHA256(pepper, secret): useless without the server-side pepper.
"""

import hashlib
import hmac
import re
import secrets
import string
from dataclasses import dataclass, field

ALPHABET = string.ascii_letters + string.digits
PREFIX_LENGTH = 12
SECRET_LENGTH = 43
KEY_PATTERN = re.compile(rf"tg_([A-Za-z0-9]{{{PREFIX_LENGTH}}})_([A-Za-z0-9]{{{SECRET_LENGTH}}})")


@dataclass(frozen=True, slots=True)
class GeneratedKey:
    prefix: str
    # repr=False: printing or logging this object never shows the secret.
    secret: str = field(repr=False)

    @property
    def full(self) -> str:
        return f"tg_{self.prefix}_{self.secret}"


def _random(length: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def generate_key() -> GeneratedKey:
    return GeneratedKey(prefix=_random(PREFIX_LENGTH), secret=_random(SECRET_LENGTH))


def parse_key(raw: str) -> tuple[str, str] | None:
    """Split a presented key into (prefix, secret), or None if it isn't a TokenGate key."""
    match = KEY_PATTERN.fullmatch(raw)
    return (match[1], match[2]) if match else None


def fingerprint(secret: str, pepper: bytes) -> bytes:
    """32-byte HMAC-SHA256 of the secret, keyed with the server-side pepper."""
    return hmac.new(pepper, secret.encode("ascii"), hashlib.sha256).digest()


def matches(secret: str, pepper: bytes, stored: bytes) -> bool:
    """Constant-time comparison, so response timing doesn't reveal how much of a guess matched."""
    return hmac.compare_digest(fingerprint(secret, pepper), stored)
