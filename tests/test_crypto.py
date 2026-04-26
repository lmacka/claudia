"""Unit tests for app/crypto.py.

Covers:
- KEK derivation determinism (same passphrase + salt = same KEK).
- DEK wrap / unwrap round-trip.
- Wrong KEK rejection (InvalidTag from cryptography).
- Break-glass envelope generation: format + entropy.
- Per-file header round-trip.
- Per-line record encrypt / decrypt.
- Tampered ciphertext rejected (AES-GCM authentication).
- Initialise + load + keys_from_passphrase happy path.
- Bad passphrase rejected at login.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from app import crypto

# ---------------------------------------------------------------------------
# KEK derivation
# ---------------------------------------------------------------------------


def test_kek_derivation_deterministic() -> None:
    salt = b"\x01" * 16
    k1 = crypto.derive_kek("passphrase-1234", salt)
    k2 = crypto.derive_kek("passphrase-1234", salt)
    assert k1 == k2
    assert len(k1) == 32


def test_kek_derivation_different_salts_different_keys() -> None:
    k1 = crypto.derive_kek("passphrase-1234", b"\x01" * 16)
    k2 = crypto.derive_kek("passphrase-1234", b"\x02" * 16)
    assert k1 != k2


def test_kek_derivation_different_passphrases_different_keys() -> None:
    salt = b"\x01" * 16
    k1 = crypto.derive_kek("passphrase-1234", salt)
    k2 = crypto.derive_kek("passphrase-5678", salt)
    assert k1 != k2


# ---------------------------------------------------------------------------
# Wrap / unwrap
# ---------------------------------------------------------------------------


def test_wrap_unwrap_roundtrip() -> None:
    kek = b"\x42" * 32
    dek = b"\x77" * 32
    wrapped = crypto.wrap_key(dek, kek)
    assert "n" in wrapped and "c" in wrapped
    recovered = crypto.unwrap_key(wrapped, kek)
    assert recovered == dek


def test_unwrap_wrong_kek_raises() -> None:
    kek = b"\x42" * 32
    wrong_kek = b"\x43" * 32
    wrapped = crypto.wrap_key(b"\x77" * 32, kek)
    with pytest.raises(InvalidTag):
        crypto.unwrap_key(wrapped, wrong_kek)


def test_wrap_produces_unique_nonce() -> None:
    """Each call to wrap_key uses a fresh nonce."""
    kek = b"\x01" * 32
    dek = b"\x02" * 32
    wraps = [crypto.wrap_key(dek, kek) for _ in range(20)]
    nonces = {w["n"] for w in wraps}
    assert len(nonces) == 20  # all unique


# ---------------------------------------------------------------------------
# Break-glass envelope
# ---------------------------------------------------------------------------


def test_envelope_format() -> None:
    text, raw = crypto.generate_break_glass_envelope()
    # 8 groups of 4 chars, hyphen-separated = 39 char total
    assert len(text) == 39
    assert text.count("-") == 7
    parts = text.split("-")
    assert len(parts) == 8
    for p in parts:
        assert len(p) == 4
        assert p.isalnum()
    assert len(raw) == 20


def test_envelope_unique() -> None:
    envelopes = {crypto.generate_break_glass_envelope()[0] for _ in range(20)}
    assert len(envelopes) == 20


def test_envelope_to_kek_roundtrip_bad_format() -> None:
    salt = b"\x01" * 16
    with pytest.raises(ValueError, match="32 base32 chars"):
        crypto.envelope_to_kek("XKVR", salt)


def test_envelope_to_kek_handles_hyphens() -> None:
    """Parent types the envelope with hyphens; we strip them."""
    salt = b"\x01" * 16
    text, _raw = crypto.generate_break_glass_envelope()
    # Should not raise — same text, just normalised
    kek1 = crypto.envelope_to_kek(text, salt)
    kek2 = crypto.envelope_to_kek(text.replace("-", ""), salt)
    kek3 = crypto.envelope_to_kek(text.lower(), salt)
    assert kek1 == kek2 == kek3
    assert len(kek1) == 32


# ---------------------------------------------------------------------------
# File header
# ---------------------------------------------------------------------------


def test_header_roundtrip() -> None:
    kek_kid = b"\x01" * 32
    kek_break = b"\x02" * 32
    header, dek = crypto.new_file_header(kek_kid, kek_break)
    assert len(dek) == 32

    line = crypto.serialise_header(header)
    parsed = crypto.parse_header(line)
    assert parsed.version == header.version
    assert parsed.wrap_kid == header.wrap_kid
    assert parsed.wrap_break == header.wrap_break

    # DEK can be unwrapped via either KEK
    recovered_kid = crypto.header_dek(parsed, kek_kid=kek_kid)
    recovered_break = crypto.header_dek(parsed, kek_break_glass=kek_break)
    assert recovered_kid == dek
    assert recovered_break == dek


def test_header_parse_bad_input() -> None:
    with pytest.raises(ValueError):
        crypto.parse_header('{"_enc": {}}')


# ---------------------------------------------------------------------------
# Per-line record encrypt / decrypt
# ---------------------------------------------------------------------------


def test_record_roundtrip() -> None:
    dek = b"\xab" * 32
    plaintext = '{"role":"user","content":"hello world"}'
    line = crypto.encrypt_record(plaintext, dek)
    recovered = crypto.decrypt_record(line, dek)
    assert recovered == plaintext


def test_record_wrong_dek_rejected() -> None:
    dek = b"\xab" * 32
    wrong_dek = b"\xac" * 32
    line = crypto.encrypt_record("plaintext", dek)
    with pytest.raises(InvalidTag):
        crypto.decrypt_record(line, wrong_dek)


def test_record_tampered_ciphertext_rejected() -> None:
    """AES-GCM authentication: flipping a base64 char in the body fails verification."""
    import json as _json

    dek = b"\xab" * 32
    line = crypto.encrypt_record("plaintext-claudia", dek)
    obj = _json.loads(line)
    ct = obj["_enc"]["c"]
    # Flip a char in the middle of the body (avoid base64 padding chars at end)
    mid = len(ct) // 2
    obj["_enc"]["c"] = ct[:mid] + ("A" if ct[mid] != "A" else "B") + ct[mid + 1 :]
    with pytest.raises(InvalidTag):
        crypto.decrypt_record(_json.dumps(obj), dek)


def test_record_unique_nonces() -> None:
    """Same DEK + same plaintext → different ciphertexts (random nonce)."""
    dek = b"\xab" * 32
    lines = [crypto.encrypt_record("identical-plaintext", dek) for _ in range(10)]
    # All distinct
    assert len(set(lines)) == 10


# ---------------------------------------------------------------------------
# Initialise + load + keys_from_passphrase
# ---------------------------------------------------------------------------


def test_initialise_and_login(tmp_path: Path) -> None:
    """Setup → login with correct passphrase → keys returned."""
    envelope = crypto.initialise_crypto(tmp_path, "longenough-passphrase")
    assert len(envelope) == 39  # 8 groups of 4 + 7 hyphens
    assert crypto.is_crypto_initialised(tmp_path)

    # Login with right passphrase
    keys = crypto.keys_from_passphrase(tmp_path, "longenough-passphrase")
    assert keys is not None
    assert len(keys.kek_kid) == 32
    assert len(keys.kek_break_glass) == 32


def test_initialise_login_wrong_passphrase(tmp_path: Path) -> None:
    crypto.initialise_crypto(tmp_path, "longenough-passphrase")
    keys = crypto.keys_from_passphrase(tmp_path, "wrong-pass-phrase")
    assert keys is None


def test_envelope_login_roundtrip(tmp_path: Path) -> None:
    """Parent break-glass: envelope alone derives KEK_break_glass."""
    envelope = crypto.initialise_crypto(tmp_path, "longenough-passphrase")
    keys_p = crypto.keys_from_passphrase(tmp_path, "longenough-passphrase")
    keys_b = crypto.keys_from_envelope(tmp_path, envelope)
    assert keys_p is not None and keys_b is not None
    assert keys_p.kek_break_glass == keys_b.kek_break_glass


def test_initialise_creates_files_with_safe_modes(tmp_path: Path) -> None:
    """kid_crypto.json and break-glass-envelope.txt should both be 0600."""
    crypto.initialise_crypto(tmp_path, "longenough-passphrase")
    crypto_p = crypto.crypto_path(tmp_path)
    env_p = crypto.envelope_path(tmp_path)
    assert (crypto_p.stat().st_mode & 0o777) == 0o600
    assert (env_p.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# End-to-end: initialise, write encrypted file, decrypt it
# ---------------------------------------------------------------------------


def test_end_to_end_encrypted_session_file(tmp_path: Path) -> None:
    """The full design D4 happy path: init → write encrypted JSONL → read back via passphrase."""
    crypto.initialise_crypto(tmp_path, "longenough-passphrase")
    keys = crypto.keys_from_passphrase(tmp_path, "longenough-passphrase")
    assert keys is not None

    # Write a session file: header + 3 records
    session_file = tmp_path / "session-001.enc.jsonl"
    header, dek = crypto.new_file_header(keys.kek_kid, keys.kek_break_glass)

    plaintext_records = [
        '{"role":"system","content":"begin"}',
        '{"role":"user","content":"hello"}',
        '{"role":"assistant","content":"hi"}',
    ]

    with session_file.open("w", encoding="utf-8") as f:
        f.write(crypto.serialise_header(header) + "\n")
        for r in plaintext_records:
            f.write(crypto.encrypt_record(r, dek) + "\n")

    # Read back: parse header, unwrap DEK, decrypt each record
    with session_file.open("r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    parsed_header = crypto.parse_header(lines[0])
    recovered_dek = crypto.header_dek(parsed_header, kek_kid=keys.kek_kid)
    assert recovered_dek == dek

    decrypted_records = [crypto.decrypt_record(line, recovered_dek) for line in lines[1:]]
    assert decrypted_records == plaintext_records


def test_end_to_end_break_glass_recovery(tmp_path: Path) -> None:
    """Parent loses passphrase, has envelope → can still read past sessions."""
    envelope = crypto.initialise_crypto(tmp_path, "longenough-passphrase")
    keys = crypto.keys_from_passphrase(tmp_path, "longenough-passphrase")
    assert keys is not None

    # Write a session under normal (kid passphrase) conditions
    header, dek = crypto.new_file_header(keys.kek_kid, keys.kek_break_glass)
    encrypted_record = crypto.encrypt_record('{"role":"user","content":"secret"}', dek)

    # Parent break-glass path: only has envelope
    parent_keys = crypto.keys_from_envelope(tmp_path, envelope)
    assert parent_keys is not None
    parsed_header = crypto.parse_header(crypto.serialise_header(header))
    recovered_dek = crypto.header_dek(parsed_header, kek_break_glass=parent_keys.kek_break_glass)
    assert recovered_dek == dek

    decrypted = crypto.decrypt_record(encrypted_record, recovered_dek)
    assert decrypted == '{"role":"user","content":"secret"}'
