from __future__ import annotations

import tempfile
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from utils.file_storage import (
    ENCRYPTED_FILE_HEADER,
    decrypt_file_payload,
    encrypt_file_payload,
    encrypt_path_in_place,
)


class EncryptedFileStorageTests(SimpleTestCase):
    def test_encrypted_payload_hides_plaintext_and_round_trips(self) -> None:
        plaintext = b"%PDF-1.7 highly sensitive signer data"

        payload = encrypt_file_payload(plaintext)

        self.assertTrue(payload.startswith(ENCRYPTED_FILE_HEADER))
        self.assertNotIn(plaintext, payload)
        self.assertEqual(decrypt_file_payload(payload), plaintext)

    def test_tampered_payload_fails_integrity_check(self) -> None:
        payload = bytearray(encrypt_file_payload(b"signature image"))
        payload[-1] = payload[-1] ^ 1

        with self.assertRaises(OSError):
            decrypt_file_payload(bytes(payload))

    def test_existing_plaintext_file_is_encrypted_in_place_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.pdf"
            path.write_bytes(b"%PDF legacy document")

            self.assertTrue(encrypt_path_in_place(path))
            encrypted = path.read_bytes()
            self.assertTrue(encrypted.startswith(ENCRYPTED_FILE_HEADER))
            self.assertEqual(decrypt_file_payload(encrypted), b"%PDF legacy document")
            self.assertFalse(encrypt_path_in_place(path))

    @override_settings(FERNET_KEY="")
    def test_encryption_requires_a_configured_key(self) -> None:
        with self.assertRaisesMessage(ImproperlyConfigured, "FERNET_KEY must be configured"):
            encrypt_file_payload(b"data")
