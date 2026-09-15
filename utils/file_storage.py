from __future__ import annotations

import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from cryptography.fernet import InvalidToken
from django.conf import settings
from django.core.files.base import ContentFile, File
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible

from utils.encryption import get_fernet


ENCRYPTED_FILE_HEADER = b"SIGNACORE-FERNET-V1\n"


def original_pdf_upload_to(instance, filename: str) -> str:
    return f"signacore/originals/{uuid.uuid4()}.pdf"


def signed_pdf_upload_to(instance, filename: str) -> str:
    return f"signacore/signed/{uuid.uuid4()}.pdf"


def signature_image_upload_to(instance, filename: str) -> str:
    return f"signacore/signatures/{uuid.uuid4()}.png"


def encrypt_file_payload(plaintext: bytes) -> bytes:
    return ENCRYPTED_FILE_HEADER + get_fernet().encrypt(plaintext)


def decrypt_file_payload(payload: bytes) -> bytes:
    if not payload.startswith(ENCRYPTED_FILE_HEADER):
        return payload
    try:
        return get_fernet().decrypt(payload[len(ENCRYPTED_FILE_HEADER) :])
    except InvalidToken as exc:
        raise OSError("Encrypted SignaCore file failed its integrity check.") from exc


def encrypt_path_in_place(path: Path) -> bool:
    if not path.is_file():
        return False
    plaintext = path.read_bytes()
    if plaintext.startswith(ENCRYPTED_FILE_HEADER):
        return False

    encrypted = encrypt_file_payload(plaintext)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".signacore-encrypt-", delete=False) as handle:
            temporary_path = Path(handle.name)
            os.chmod(temporary_path, 0o600)
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return True


@deconstructible
class EncryptedFileSystemStorage(FileSystemStorage):
    def _save(self, name: str, content) -> str:
        plaintext = content.read()
        encrypted_content = ContentFile(encrypt_file_payload(plaintext), name=Path(name).name)
        return super()._save(name, encrypted_content)

    def _open(self, name: str, mode: str = "rb") -> File:
        if mode not in {"r", "rb"}:
            raise ValueError("Encrypted SignaCore files are read-only through the storage API.")
        with super()._open(name, "rb") as encrypted_file:
            plaintext = decrypt_file_payload(encrypted_file.read())
        return ContentFile(plaintext, name=name)


encrypted_file_storage = EncryptedFileSystemStorage()


def _temporary_root() -> Path:
    root = Path(getattr(settings, "SIGNACORE_TEMP_ROOT", tempfile.gettempdir()))
    if not root.exists():
        root.mkdir(mode=0o700, parents=True)
    elif not root.is_dir():
        raise NotADirectoryError(f"SignaCore temporary root is not a directory: {root}")
    return root


@contextmanager
def temporary_plaintext_file(field_file, *, suffix: str) -> Iterator[Path]:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=_temporary_root(),
            prefix="signacore-plaintext-",
            suffix=suffix,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            os.chmod(temporary_path, 0o600)
            field_file.open("rb")
            try:
                for chunk in field_file.chunks():
                    handle.write(chunk)
            finally:
                field_file.close()
        yield temporary_path
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@contextmanager
def temporary_output_file(*, suffix: str) -> Iterator[Path]:
    temporary_path: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            dir=_temporary_root(),
            prefix="signacore-output-",
            suffix=suffix,
        )
        os.close(descriptor)
        temporary_path = Path(raw_path)
        os.chmod(temporary_path, 0o600)
        yield temporary_path
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def save_encrypted_field_file(field_file, plaintext_path: Path, *, filename: str) -> None:
    with plaintext_path.open("rb") as handle:
        field_file.save(filename, File(handle), save=False)
