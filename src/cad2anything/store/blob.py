"""Mirror workspace files to an Azure Blob container using Microsoft Entra authentication."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

DEFAULT_PREFIXES = ("models/", "changes/", "reports/", "business/", "drawings/", "artifacts/")


def _credential() -> Any:
    from azure.identity import DefaultAzureCredential

    client_id = os.environ.get("AZURE_CLIENT_ID")
    return (
        DefaultAzureCredential(managed_identity_client_id=client_id)
        if client_id
        else DefaultAzureCredential()
    )


class BlobMirror:
    def __init__(self, account: str, container: str, credential: Any | None = None):
        from azure.storage.blob import BlobServiceClient

        url = f"https://{account}.blob.core.windows.net"
        service = BlobServiceClient(url, credential=credential or _credential())
        self.container = service.get_container_client(container)

    @classmethod
    def from_env(cls) -> BlobMirror | None:
        account = os.environ.get("DATA_STORAGE_ACCOUNT")
        if not account:
            return None
        return cls(account, os.environ.get("DATA_CONTAINER", "data"))

    def push(self, path: Path, root: Path) -> None:
        name = Path(path).relative_to(root).as_posix()
        with Path(path).open("rb") as handle:
            self.container.upload_blob(name, handle, overwrite=True)

    def push_tree(self, root: Path, prefixes: tuple[str, ...] = DEFAULT_PREFIXES) -> int:
        count = 0
        for path in sorted(p for p in Path(root).rglob("*") if p.is_file()):
            name = path.relative_to(root).as_posix()
            if name.startswith(prefixes):
                self.push(path, root)
                count += 1
        return count

    def pull(self, root: Path, prefixes: tuple[str, ...] = DEFAULT_PREFIXES) -> int:
        count = 0
        for prefix in prefixes:
            for blob in self.container.list_blobs(name_starts_with=prefix):
                target = Path(root) / blob.name
                if target.exists() and target.stat().st_size == blob.size:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as handle:
                    handle.write(self.container.download_blob(blob.name).readall())
                count += 1
        return count
