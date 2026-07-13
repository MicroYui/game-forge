"""Object-store adapters for immutable GameForge payloads."""

from gameforge.runtime.object_store.local import LocalFileOps, LocalObjectStore

__all__ = ["LocalFileOps", "LocalObjectStore"]
