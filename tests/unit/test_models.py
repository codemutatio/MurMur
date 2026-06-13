"""Tests for murmur.transcribe.models.ModelManager — T-15, R-24, E-18."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from huggingface_hub.errors import LocalEntryNotFoundError

from murmur.transcribe.models import MODEL_REPOS, ChecksumError, ModelManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_sibling(filename: str, sha256: str | None, size: int | None = None) -> MagicMock:
    """Create a fake HF sibling (file metadata object)."""
    sibling = MagicMock()
    sibling.rfilename = filename
    if sha256 is not None:
        lfs = MagicMock()
        lfs.sha256 = sha256
        sibling.lfs = lfs
    else:
        sibling.lfs = None
    sibling.size = size
    return sibling


def _make_fake_api(siblings: list[MagicMock]) -> MagicMock:
    """Create a fake HfApi whose model_info returns the given siblings."""
    api = MagicMock()
    info = MagicMock()
    info.siblings = siblings
    api.model_info.return_value = info
    return api


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_unknown_model_raises(tmp_path: Path) -> None:
    """ensure() raises ValueError for an unknown model name."""
    manager = ModelManager(cache_dir=tmp_path)
    with (
        pytest.raises(ValueError, match="Unknown model"),
        patch(
            "murmur.transcribe.models.ModelManager._download",
            return_value=str(tmp_path / "snapshot"),
        ),
    ):
        manager.ensure("not-a-model")


def test_checksum_pass_writes_marker(tmp_path: Path) -> None:  # [R-24]
    """Successful sha256 verification writes a verified-marker JSON file."""
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    model_file = snapshot / "weights.bin"
    file_content = b"fake model weights"
    model_file.write_bytes(file_content)
    expected_sha = _sha256(file_content)

    siblings = [_make_sibling("weights.bin", expected_sha)]
    api = _make_fake_api(siblings)

    manager = ModelManager(cache_dir=tmp_path, api=api)

    with patch(
        "murmur.transcribe.models.ModelManager._download",
        return_value=str(snapshot),
    ):
        result = manager.ensure("large-v3")

    assert result == str(snapshot)
    marker = tmp_path / "murmur-model-verified-large-v3.json"
    assert marker.exists(), "Verified-marker file should have been written"
    data = json.loads(marker.read_text())
    assert data["model"] == "large-v3"
    assert data["verified"] is True


def test_mismatch_deletes_and_raises(tmp_path: Path) -> None:  # [E-18]
    """SHA256 mismatch: deletes snapshot dir and raises ChecksumError."""
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    model_file = snapshot / "weights.bin"
    model_file.write_bytes(b"correct bytes")

    bad_sha = "a" * 64  # Wrong sha256
    siblings = [_make_sibling("weights.bin", bad_sha)]
    api = _make_fake_api(siblings)

    manager = ModelManager(cache_dir=tmp_path, api=api)

    with (
        patch(
            "murmur.transcribe.models.ModelManager._download",
            return_value=str(snapshot),
        ),
        pytest.raises(ChecksumError, match="Checksum mismatch"),
    ):
        manager.ensure("large-v3")

    # Snapshot directory should have been deleted
    assert not snapshot.exists(), "Snapshot dir should be deleted on checksum failure"


def test_marker_skips_rehash(tmp_path: Path) -> None:
    """If a verified-marker exists, ensure() skips sha256 verification (fast startup)."""
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()

    # Write the marker directly
    marker = tmp_path / "murmur-model-verified-large-v3.json"
    marker.write_text(json.dumps({"model": "large-v3", "verified": True}))

    api = MagicMock()  # Should NOT be called if marker present
    manager = ModelManager(cache_dir=tmp_path, api=api)

    # With the marker present the snapshot is resolved offline; verification (and
    # thus the network-touching HfApi) must never run.
    with patch(
        "murmur.transcribe.models.ModelManager._resolve_offline",
        return_value=str(snapshot),
    ):
        result = manager.ensure("large-v3")

    assert result == str(snapshot)
    api.model_info.assert_not_called()  # Verify was skipped


def test_marker_present_resolves_offline_no_network(tmp_path: Path) -> None:
    """With the marker present, ensure() resolves the snapshot via
    snapshot_download(local_files_only=True) and never touches the network.

    Proves the privacy constraint (spec Constraints): no outbound call on a
    normal startup once the model has been verified on a prior run. The fake
    network collaborator (HfApi.model_info) must never be invoked, and the
    downloader must be asked for a local-only resolution.
    """
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()

    marker = tmp_path / "murmur-model-verified-large-v3.json"
    marker.write_text(json.dumps({"model": "large-v3", "verified": True}))

    api = MagicMock()  # network verification collaborator — must stay untouched
    manager = ModelManager(cache_dir=tmp_path, api=api)

    fake_snapshot_download = MagicMock(return_value=str(snapshot))
    with patch("huggingface_hub.snapshot_download", fake_snapshot_download):
        result = manager.ensure("large-v3")

    assert result == str(snapshot)
    # The downloader was consulted exactly once, in offline mode.
    fake_snapshot_download.assert_called_once()
    _, kwargs = fake_snapshot_download.call_args
    assert kwargs.get("local_files_only") is True, (
        "cached resolution must pass local_files_only=True so huggingface_hub "
        "makes no outbound request (no remote revision resolution)"
    )
    # No network verification path was entered.
    api.model_info.assert_not_called()


def test_marker_present_but_snapshot_missing_falls_back(tmp_path: Path) -> None:
    """If the marker exists but the local snapshot is gone, ensure() falls back
    to a network download and re-verifies — the marker is not blindly trusted
    when the cache is missing."""
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    content = b"recovered weights"
    (snapshot / "weights.bin").write_bytes(content)
    expected_sha = _sha256(content)

    marker = tmp_path / "murmur-model-verified-large-v3.json"
    marker.write_text(json.dumps({"model": "large-v3", "verified": True}))

    api = _make_fake_api([_make_sibling("weights.bin", expected_sha)])
    manager = ModelManager(cache_dir=tmp_path, api=api)

    calls: list[dict[str, object]] = []

    def fake_snapshot_download(**kwargs: object) -> str:
        calls.append(kwargs)
        if kwargs.get("local_files_only"):
            raise LocalEntryNotFoundError("snapshot not cached")
        return str(snapshot)

    with patch("huggingface_hub.snapshot_download", side_effect=fake_snapshot_download):
        result = manager.ensure("large-v3")

    assert result == str(snapshot)
    # Offline attempt first (local_files_only=True), then the network fallback.
    assert len(calls) == 2
    assert calls[0].get("local_files_only") is True
    assert "local_files_only" not in calls[1]
    # Re-verification ran against HF metadata on the re-downloaded snapshot.
    api.model_info.assert_called_once()


def test_progress_reported(tmp_path: Path) -> None:
    """Progress callback receives (done, total) calls during download."""
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    file_content = b"model data"
    (snapshot / "model.bin").write_bytes(file_content)
    expected_sha = _sha256(file_content)

    siblings = [_make_sibling("model.bin", expected_sha)]
    api = _make_fake_api(siblings)

    progress_calls: list[tuple[int, int]] = []

    manager = ModelManager(
        cache_dir=tmp_path,
        api=api,
        progress=lambda done, total: progress_calls.append((done, total)),
    )

    # Mock _download to call the progress hook once (simulating tqdm progress)
    def fake_download(repo: str) -> str:
        if manager._progress:
            manager._progress(50, 100)
            manager._progress(100, 100)
        return str(snapshot)

    with patch.object(manager, "_download", side_effect=fake_download):
        manager.ensure("large-v3")

    assert len(progress_calls) > 0, "Progress callback should have been called"
    assert progress_calls[-1] == (100, 100)


def test_no_lfs_metadata_size_ok(tmp_path: Path) -> None:
    """File with no LFS metadata but matching size passes with a logged warning."""
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    file_content = b"small file"
    (snapshot / "config.json").write_bytes(file_content)

    # Sibling has no LFS metadata but correct size
    sibling = _make_sibling("config.json", sha256=None, size=len(file_content))
    api = _make_fake_api([sibling])

    manager = ModelManager(cache_dir=tmp_path, api=api)

    with patch(
        "murmur.transcribe.models.ModelManager._download",
        return_value=str(snapshot),
    ):
        result = manager.ensure("large-v3")

    assert result == str(snapshot)


def test_no_lfs_sha256_field_skipped(tmp_path: Path) -> None:
    """File with LFS metadata but no sha256 field is skipped gracefully."""
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "model.bin").write_bytes(b"data")

    sibling = MagicMock()
    sibling.rfilename = "model.bin"
    lfs = MagicMock()
    lfs.sha256 = None  # sha256 field is None
    sibling.lfs = lfs
    sibling.size = 4

    api = _make_fake_api([sibling])
    manager = ModelManager(cache_dir=tmp_path, api=api)

    with patch(
        "murmur.transcribe.models.ModelManager._download",
        return_value=str(snapshot),
    ):
        result = manager.ensure("large-v3")

    assert result == str(snapshot)


def test_model_repos_known() -> None:
    """MODEL_REPOS contains expected model keys."""
    assert "large-v3" in MODEL_REPOS
    assert "large-v3-turbo" in MODEL_REPOS
