"""Password hashing — stdlib scrypt, no extra dependency."""
import hashlib
import hmac
import os
import secrets


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + "$" + digest.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def temp_password() -> str:
    """One-time password the admin passes to a new consultant. It is single-use — the
    consultant sets their own password on first sign-in (must_change_password), so this is
    never a standing credential. A readable word prefix + ~72 bits of entropy (token_urlsafe)
    so it can't be guessed even before the forced rotation."""
    word = secrets.choice(["amber", "cedar", "delta", "ember", "flint", "grove", "harbor", "ivory"])
    return f"{word}-{secrets.token_urlsafe(9)}"
