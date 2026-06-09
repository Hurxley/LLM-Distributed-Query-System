"""
HMAC-SHA256 tokenizer for person ID blinding.

Same salt + same id_card = same token (cross-DB alignment)
Without salt, token is irreversible (SHA256 preimage resistance).
"""

import hashlib
import hmac
import os


def get_salt() -> str:
    """Get salt from environment variable. Must be set before worker starts."""
    salt = os.environ.get('SALT', '')
    if not salt:
        raise RuntimeError("SALT environment variable is not set. "
                           "All workers must share the same salt.")
    return salt


def tokenize(id_card: str) -> str:
    """Convert an ID card number to a blinded token."""
    salt = get_salt()
    return hmac.new(
        salt.encode('utf-8'),
        id_card.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
