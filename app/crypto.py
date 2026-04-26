"""
Kid-mode encryption: AES-GCM at-rest with envelope-wrapped DEKs.

Key hierarchy (per /plan-eng-review D2 + D4):

    kid passphrase -> Argon2id(salt) -> KEK_kid          (32 bytes)
    break-glass envelope -> Argon2id(salt) -> KEK_break_glass  (32 bytes)
    each file -> random DEK                              (32 bytes)
        DEK wrapped with KEK_kid          -> per-file header
        DEK wrapped with KEK_break_glass  -> per-file header

On-disk layout per encrypted JSONL file:

    Line 1 (header, JSON):
      {"_header": {
        "v": 1,
        "ts": "2026-04-26T...",
        "wrap_kid":   {"n": "<b64-nonce>", "c": "<b64-ct>"},
        "wrap_break": {"n": "<b64-nonce>", "c": "<b64-ct>"}
      }}

    Line 2..N (records, JSON):
      {"_enc": {"n": "<b64-nonce>", "c": "<b64-ciphertext-w-tag>"}}

Each record encrypts the *plaintext JSONL of the original event* — i.e. for
storage.py, the same JSON line that adult mode would write directly is
encrypted here.

Break-glass envelope display format: 8 groups of 4 base32 characters,
hyphen-separated. Stable, scannable, fits a printed page.

Salt + break-glass-wrap-of-kid-key live at:
    /data/.credentials/kid_crypto.json
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import structlog
from argon2.low_level import Type as Argon2Type
from argon2.low_level import hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Argon2id parameters tuned for ~250ms / 64MB on a modern x86 core.
# The slowness is the brute-force defense (paired with /login rate limit).
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 65536  # KiB
ARGON2_PARALLELISM = 2

KEK_LENGTH = 32  # 256-bit AES
DEK_LENGTH = 32
NONCE_LENGTH = 12  # 96-bit, AES-GCM standard
SALT_LENGTH = 16

CRYPTO_FILE = ".credentials/kid_crypto.json"
ENVELOPE_FILE = ".credentials/break_glass_envelope.txt"  # written once at setup
FORMAT_VERSION = 1


# ---------------------------------------------------------------------------
# KEK derivation
# ---------------------------------------------------------------------------


def derive_kek(passphrase: str, salt: bytes) -> bytes:
    """Argon2id, returns 32-byte KEK."""
    return hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
        hash_len=KEK_LENGTH,
        type=Argon2Type.ID,
    )


# ---------------------------------------------------------------------------
# Wrap / unwrap DEK with a KEK
# ---------------------------------------------------------------------------


def wrap_key(plaintext_key: bytes, kek: bytes) -> dict[str, str]:
    """AES-GCM-encrypt a key with a KEK. Returns {n: nonce, c: ciphertext+tag} as base64."""
    nonce = secrets.token_bytes(NONCE_LENGTH)
    aes = AESGCM(kek)
    ct = aes.encrypt(nonce, plaintext_key, None)
    return {
        "n": base64.b64encode(nonce).decode("ascii"),
        "c": base64.b64encode(ct).decode("ascii"),
    }


def unwrap_key(wrapped: dict[str, str], kek: bytes) -> bytes:
    """Reverse of wrap_key. Raises InvalidTag on bad KEK."""
    nonce = base64.b64decode(wrapped["n"])
    ct = base64.b64decode(wrapped["c"])
    aes = AESGCM(kek)
    return aes.decrypt(nonce, ct, None)


# ---------------------------------------------------------------------------
# Break-glass envelope (printed once at setup, stored offline by parent)
# ---------------------------------------------------------------------------


def generate_break_glass_envelope() -> tuple[str, bytes]:
    """
    Returns (envelope_text_for_display, raw_key_bytes).

    envelope_text: 8 groups of 4 base32 chars (e.g. "XKVR-83HQ-MPNB-9LFE-4TC2-WS6Y-HJ5K-NQDA").
    raw_key_bytes: 20 bytes of entropy (160 bits, fits 8x4 base32 cleanly).

    The printed envelope is the only out-of-band recovery vector. Parent
    must store it offline (family safe / password manager). If the kid
    forgets their passphrase AND the parent loses the envelope, data is
    unrecoverable. Documented in docs/safety.md (writes Step 6f).
    """
    raw = secrets.token_bytes(20)
    b32 = base64.b32encode(raw).decode("ascii").rstrip("=")  # 32 chars
    groups = [b32[i : i + 4] for i in range(0, 32, 4)]
    return "-".join(groups), raw


def envelope_to_kek(envelope: str, salt: bytes) -> bytes:
    """
    Parent types the printed envelope; we derive KEK_break_glass.
    Strips hyphens + whitespace + uppercase first.
    """
    cleaned = envelope.replace("-", "").replace(" ", "").upper()
    if len(cleaned) != 32:
        raise ValueError(f"envelope must be 32 base32 chars (got {len(cleaned)})")
    # 32 base32 chars = 20 raw bytes, no padding needed (32 is mult of 8).
    raw = base64.b32decode(cleaned)
    return derive_kek(raw.hex(), salt)  # treat hex of raw as the "passphrase"


# ---------------------------------------------------------------------------
# kid_crypto.json — setup + load
# ---------------------------------------------------------------------------


@dataclass
class KidCryptoState:
    salt: bytes  # for Argon2id KEK derivation
    break_glass_wrapped: dict[str, str]  # break-glass raw bytes, wrapped with KEK_kid
    sentinel_wrapped: dict[str, str]  # known plaintext, wrapped with KEK_kid (verifies passphrase decrypts)


_SENTINEL_PLAINTEXT = b"claudia-kek-ok-v1"


def crypto_path(data_root: Path) -> Path:
    return data_root / CRYPTO_FILE


def envelope_path(data_root: Path) -> Path:
    return data_root / ENVELOPE_FILE


def is_crypto_initialised(data_root: Path) -> bool:
    return crypto_path(data_root).exists()


def initialise_crypto(data_root: Path, passphrase: str) -> str:
    """
    First-time setup. Returns the printed envelope text for the parent.

    Side effects:
      - /data/.credentials/kid_crypto.json (the wrapped break-glass key
        and a sentinel to verify the kid's passphrase later)
      - /data/.credentials/break_glass_envelope.txt (the parent's
        printable copy; intended to be displayed once and then deleted
        from disk after the parent confirms they printed it. v1 we keep
        it on disk so the parent can re-print until they explicitly
        confirm. TODO v1.5: explicit "I printed it" → wipe).
    """
    salt = secrets.token_bytes(SALT_LENGTH)
    kek_kid = derive_kek(passphrase, salt)

    envelope_text, envelope_raw = generate_break_glass_envelope()
    # Wrap the raw envelope bytes (not the derived KEK) so that recovery
    # path doesn't depend on remembering the salt encoding scheme.
    break_glass_wrapped = wrap_key(envelope_raw, kek_kid)
    sentinel_wrapped = wrap_key(_SENTINEL_PLAINTEXT, kek_kid)

    state = {
        "salt": base64.b64encode(salt).decode("ascii"),
        "break_glass_wrapped": break_glass_wrapped,
        "sentinel_wrapped": sentinel_wrapped,
        "created_at": time.time(),
    }
    path = crypto_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)

    # Display copy of the envelope. v1 we write it to disk so the parent
    # can come back and re-view; v1.5 will add an "I printed it" wipe.
    env_path = envelope_path(data_root)
    env_path.write_text(envelope_text + "\n", encoding="utf-8")
    env_path.chmod(0o600)

    log.info("crypto.initialised")
    return envelope_text


def load_crypto_state(data_root: Path) -> KidCryptoState:
    """Reads kid_crypto.json. Raises FileNotFoundError if missing."""
    path = crypto_path(data_root)
    state = json.loads(path.read_text(encoding="utf-8"))
    return KidCryptoState(
        salt=base64.b64decode(state["salt"]),
        break_glass_wrapped=state["break_glass_wrapped"],
        sentinel_wrapped=state["sentinel_wrapped"],
    )


# ---------------------------------------------------------------------------
# Login: derive KEK_kid + KEK_break_glass from kid passphrase
# ---------------------------------------------------------------------------


@dataclass
class SessionKeys:
    kek_kid: bytes
    kek_break_glass: bytes


def keys_from_passphrase(data_root: Path, passphrase: str) -> SessionKeys | None:
    """
    Returns SessionKeys if the passphrase is correct (sentinel decrypts),
    else None. Used at /login.
    """
    state = load_crypto_state(data_root)
    kek_kid = derive_kek(passphrase, state.salt)
    try:
        sentinel = unwrap_key(state.sentinel_wrapped, kek_kid)
    except Exception:
        return None
    if sentinel != _SENTINEL_PLAINTEXT:
        return None

    # Unwrap break-glass envelope bytes, derive KEK_break_glass with same salt
    envelope_raw = unwrap_key(state.break_glass_wrapped, kek_kid)
    kek_break_glass = derive_kek(envelope_raw.hex(), state.salt)
    return SessionKeys(kek_kid=kek_kid, kek_break_glass=kek_break_glass)


def keys_from_envelope(data_root: Path, envelope_text: str) -> SessionKeys | None:
    """
    Parent break-glass path. Parent enters the printed envelope key
    (with hyphens), we derive KEK_break_glass and ALSO recover KEK_kid
    by unwrapping it from a future-stored "kid-recovery" record (not yet
    implemented; v1.1 break-glass-reset flow).

    For v1 this returns just the break-glass KEK; the parent can decrypt
    past sessions but cannot create new ones until the kid sets a new
    passphrase (which re-runs initialise_crypto).
    """
    state = load_crypto_state(data_root)
    try:
        kek_break_glass = envelope_to_kek(envelope_text, state.salt)
    except ValueError:
        return None

    # Verify by unwrapping the break-glass-wrapped record. We don't store
    # a sentinel_wrapped_with_break_glass in v1 so this isn't a strict
    # check — we'd find out at decrypt time. Live with that.
    return SessionKeys(kek_kid=b"", kek_break_glass=kek_break_glass)


# ---------------------------------------------------------------------------
# Per-file encrypted JSONL: header + per-line records
# ---------------------------------------------------------------------------


@dataclass
class FileHeader:
    version: int
    created_at: str
    wrap_kid: dict[str, str]  # DEK wrapped with KEK_kid
    wrap_break: dict[str, str]  # DEK wrapped with KEK_break_glass


def new_file_header(kek_kid: bytes, kek_break_glass: bytes) -> tuple[FileHeader, bytes]:
    """Returns (header, raw_DEK). DEK is needed by encrypt_record for the file."""
    dek = secrets.token_bytes(DEK_LENGTH)
    header = FileHeader(
        version=FORMAT_VERSION,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        wrap_kid=wrap_key(dek, kek_kid),
        wrap_break=wrap_key(dek, kek_break_glass),
    )
    return header, dek


def serialise_header(header: FileHeader) -> str:
    """JSONL line for the file header (line 1 of an encrypted JSONL)."""
    return json.dumps(
        {
            "_header": {
                "v": header.version,
                "ts": header.created_at,
                "wrap_kid": header.wrap_kid,
                "wrap_break": header.wrap_break,
            }
        }
    )


def parse_header(line: str) -> FileHeader:
    """Inverse of serialise_header. Raises ValueError on bad header."""
    data = json.loads(line)
    h = data.get("_header")
    if not h:
        raise ValueError("not a claudia encrypted-JSONL header")
    return FileHeader(
        version=int(h["v"]),
        created_at=str(h["ts"]),
        wrap_kid=h["wrap_kid"],
        wrap_break=h["wrap_break"],
    )


def header_dek(header: FileHeader, kek_kid: bytes | None = None, kek_break_glass: bytes | None = None) -> bytes:
    """Unwrap the file's DEK using whichever KEK is provided."""
    if kek_kid:
        return unwrap_key(header.wrap_kid, kek_kid)
    if kek_break_glass:
        return unwrap_key(header.wrap_break, kek_break_glass)
    raise ValueError("must provide kek_kid or kek_break_glass")


def encrypt_record(plaintext: str, dek: bytes) -> str:
    """Encrypt a JSONL record. Returns a JSONL line (string with no trailing newline)."""
    nonce = secrets.token_bytes(NONCE_LENGTH)
    aes = AESGCM(dek)
    ct = aes.encrypt(nonce, plaintext.encode("utf-8"), None)
    return json.dumps(
        {
            "_enc": {
                "n": base64.b64encode(nonce).decode("ascii"),
                "c": base64.b64encode(ct).decode("ascii"),
            }
        }
    )


def decrypt_record(line: str, dek: bytes) -> str:
    """Inverse of encrypt_record. Raises ValueError on bad shape, InvalidTag on bad DEK."""
    data = json.loads(line)
    enc = data.get("_enc")
    if not enc:
        raise ValueError("not a claudia encrypted record")
    nonce = base64.b64decode(enc["n"])
    ct = base64.b64decode(enc["c"])
    aes = AESGCM(dek)
    pt = aes.decrypt(nonce, ct, None)
    return pt.decode("utf-8")
