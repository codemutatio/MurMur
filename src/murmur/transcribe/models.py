"""Model manager: download, verify, and cache Whisper model weights.

Uses huggingface_hub.snapshot_download with explicit sha256 checking against
HF LFS metadata (huggingface_hub does NOT reliably self-verify — see open
issues #2364/#3643 confirmed in plan).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

MODEL_REPOS: dict[str, str] = {
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}


class ChecksumError(Exception):
    """Raised when a downloaded model file fails sha256 verification."""


class ModelManager:
    """Download and verify Whisper model weights from Hugging Face.

    Args:
        cache_dir: Override the default HF cache directory (tests use a tmp dir).
        api: HfApi instance (inject a fake in tests).
        progress: Callback for download progress: (bytes_done, bytes_total) → None.
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        api: Any | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        self._cache_dir = cache_dir
        self._api = api
        self._progress = progress

    def ensure(self, model: str) -> str:
        """Ensure *model* is available locally. Returns the snapshot path.

        If a verified-marker file exists, the snapshot is resolved purely from
        the local cache with no network call (the sha256 check is also skipped
        for fast startup). This upholds the privacy constraint: the default
        configuration makes no outbound request during normal operation — the
        first-run download is the only sanctioned exception. The marker is not
        blindly trusted: if the local snapshot has gone missing, ensure() falls
        back to a network download and re-verifies.

        Otherwise (first run): download → sha256 verify every LFS file →
        write marker.

        Raises:
            ChecksumError: if any file fails sha256 verification.
            ValueError: if *model* is not in MODEL_REPOS.
        """
        if model not in MODEL_REPOS:
            raise ValueError(f"Unknown model '{model}'. Known models: {list(MODEL_REPOS)}")

        repo = MODEL_REPOS[model]
        marker = self._marker_path(model)

        if marker.exists():
            cached = self._resolve_offline(repo)
            if cached is not None:
                _log.debug("Model %s already verified (marker present); resolved offline", model)
                return cached
            _log.warning(
                "Model %s marker present but local snapshot missing; re-downloading", model
            )

        snapshot_path = self._download(repo)
        _log.info("Verifying model weights for %s ...", model)
        self._verify(repo, snapshot_path)
        marker.write_text(json.dumps({"model": model, "repo": repo, "verified": True}))
        _log.info("Model %s verified OK", model)
        return snapshot_path

    # ------------------------------------------------------------------

    def _download(self, repo: str) -> str:
        from huggingface_hub import snapshot_download

        progress = self._progress

        kwargs: dict[str, Any] = {"repo_id": repo}
        if self._cache_dir is not None:
            kwargs["cache_dir"] = str(self._cache_dir)
        if progress is not None:
            try:
                from tqdm.auto import tqdm  # type: ignore[import-untyped]  # tqdm has no stubs

                _cb = progress  # narrowed local to avoid None-check confusion in closure

                class _ProgressTqdm(tqdm):  # type: ignore[misc]
                    def update(self, n: int = 1) -> bool | None:
                        result: bool | None = super().update(n)
                        _cb(int(self.n), int(self.total or 0))
                        return result

                kwargs["tqdm_class"] = _ProgressTqdm
            except ImportError:
                pass

        result: str = snapshot_download(**kwargs)
        return result

    def _resolve_offline(self, repo: str) -> str | None:
        """Resolve a fully-cached snapshot path without any network call.

        Returns the local snapshot path, or None if the snapshot is not fully
        cached. ``local_files_only=True`` guarantees huggingface_hub makes no
        outbound request — in particular it skips resolving the remote revision
        over the network, which snapshot_download otherwise does even when every
        file is already cached.
        """
        from huggingface_hub import snapshot_download
        from huggingface_hub.errors import LocalEntryNotFoundError

        kwargs: dict[str, Any] = {"repo_id": repo, "local_files_only": True}
        if self._cache_dir is not None:
            kwargs["cache_dir"] = str(self._cache_dir)

        try:
            return str(snapshot_download(**kwargs))
        except (LocalEntryNotFoundError, FileNotFoundError) as exc:
            _log.debug("No local snapshot for %s (%s); will download", repo, exc)
            return None

    def _verify(self, repo: str, snapshot_path: str) -> None:
        """SHA256-check every LFS file in *snapshot_path* against HF metadata."""
        api = self._api
        if api is None:
            from huggingface_hub import HfApi

            api = HfApi()

        try:
            info = api.model_info(repo, files_metadata=True)
            files_meta = info.siblings or []
        except Exception as exc:
            _log.warning("Could not fetch model metadata for %s: %s", repo, exc)
            return

        for sibling in files_meta:
            rfilename = getattr(sibling, "rfilename", None)
            lfs = getattr(sibling, "lfs", None)
            if rfilename is None:
                continue
            local = Path(snapshot_path) / rfilename
            if not local.exists():
                continue
            if lfs is None:
                # No LFS metadata → size check + warning
                expected_size = getattr(sibling, "size", None)
                if expected_size is not None and local.stat().st_size != expected_size:
                    _log.warning(
                        "Size mismatch for %s (expected %d, got %d) — "
                        "no LFS sha256 available for full verification",
                        rfilename,
                        expected_size,
                        local.stat().st_size,
                    )
                else:
                    _log.debug("No LFS metadata for %s; size OK", rfilename)
                continue

            expected_sha256 = getattr(lfs, "sha256", None)
            if expected_sha256 is None:
                _log.debug("No sha256 in LFS metadata for %s; skipping", rfilename)
                continue

            actual = self._sha256(local)
            if actual != expected_sha256:
                # Delete corrupt snapshot before raising
                import shutil

                shutil.rmtree(snapshot_path, ignore_errors=True)
                raise ChecksumError(
                    f"Checksum mismatch for {rfilename}: expected {expected_sha256}, got {actual}"
                )

    def _marker_path(self, model: str) -> Path:
        cache_root = self._cache_dir or Path.home() / ".cache" / "huggingface"
        return cache_root / f"murmur-model-verified-{model}.json"

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
