"""File storage: STORAGE_BACKEND=local|gcs (bucket from GCS_BUCKET). Used for F1 audio and evidence files.

save(kind, data, content_type, ext) -> {"uri", "path", "bytes"}; kind is "audio" or "evidence".
Objects are named <kind>/<YYYY-MM-DD>/<sha256-prefix>.<ext>, so the same file is stored once.
"""
import hashlib
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KINDS = {"audio", "evidence"}


def object_name(kind: str, data: bytes, ext: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    return f"{kind}/{datetime.now(timezone.utc):%Y-%m-%d}/{hashlib.sha256(data).hexdigest()[:16]}.{ext.lstrip('.')}"


class Storage(ABC):
    backend: str

    @abstractmethod
    def save(self, kind: str, data: bytes, content_type: str, ext: str) -> dict: ...

    @abstractmethod
    def read(self, uri: str) -> bytes: ...


class LocalStorage(Storage):
    backend = "local"

    def __init__(self, root: Path | None = None):
        self.root = root or ROOT / "storage"

    def save(self, kind, data, content_type, ext) -> dict:
        name = object_name(kind, data, ext)
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {"uri": f"file://{path}", "path": name, "bytes": len(data)}

    def read(self, uri: str) -> bytes:
        return Path(uri.removeprefix("file://")).read_bytes()


class GCSStorage(Storage):
    backend = "gcs"

    def __init__(self, bucket: str | None = None, client=None):
        from google.cloud import storage

        self.bucket_name = bucket or os.getenv("GCS_BUCKET")
        if not self.bucket_name:
            raise RuntimeError("STORAGE_BACKEND=gcs needs GCS_BUCKET")
        self.bucket = (client or storage.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT"))).bucket(self.bucket_name)

    def save(self, kind, data, content_type, ext) -> dict:
        name = object_name(kind, data, ext)
        self.bucket.blob(name).upload_from_string(data, content_type=content_type)
        return {"uri": f"gs://{self.bucket_name}/{name}", "path": name, "bytes": len(data)}

    def read(self, uri: str) -> bytes:
        return self.bucket.blob(uri.split(f"gs://{self.bucket_name}/", 1)[1]).download_as_bytes()


_cached: dict[str, Storage] = {}


def get_storage() -> Storage:
    backend = os.getenv("STORAGE_BACKEND", "local").lower()
    if backend not in _cached:
        _cached[backend] = GCSStorage() if backend == "gcs" else LocalStorage()
    return _cached[backend]
