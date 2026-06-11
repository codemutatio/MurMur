"""Tests for murmur.transcribe.models.ModelManager — T-15, R-24, E-18."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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

    with patch(
        "murmur.transcribe.models.ModelManager._download",
        return_value=str(snapshot),
    ):
        result = manager.ensure("large-v3")

    assert result == str(snapshot)
    api.model_info.assert_not_called()  # Verify was skipped


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
