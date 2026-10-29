"""Backup failure contracts; use local files, never production DB/COS."""
import hashlib
import multiprocessing
import os
from pathlib import Path
import subprocess

import pytest

from scripts import backup_db_to_cos as backup


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    stage, remote = tmp_path / "stage", tmp_path / "remote"
    stage.mkdir()
    remote.mkdir()
    monkeypatch.setattr(backup, "PART_BYTES", 8)
    monkeypatch.setattr(backup, "cos_cache_policy", lambda _: ([stage], 0))
    monkeypatch.setattr(backup, "check_upload_space", lambda *_: None)
    # Only replace the PostgreSQL decoder; upload/hash/manifest logic stays real.
    monkeypatch.setattr(backup, "run", lambda *_args, **_kwargs: None)
    return stage, remote


def make_dump(stage, timestamp="20261028_031701"):
    path = stage / f"stock_backup_{timestamp}.dump"
    path.write_bytes(b"nontrivial complete backup data")
    return path


def test_upload_checks_bytes_then_removes_local(sandbox):
    stage, remote = sandbox
    path = make_dump(stage)
    content = path.read_bytes()
    manifest = backup.upload(path, remote, [stage], 0)
    assert not path.exists()
    backup.verify_remote(manifest, len(content), hashlib.sha256(content).hexdigest())
    assert len(backup.read_manifest(manifest)[2]) == 4


def test_upload_failure_keeps_dump_and_does_not_publish(sandbox, monkeypatch):
    stage, remote = sandbox
    path = make_dump(stage)
    original = Path.open

    def failing_open(self, mode="r", *args, **kwargs):
        if self.name.endswith("part-ab") and mode == "wb":
            raise OSError("upload EIO")
        return original(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(OSError, match="upload EIO"):
        backup.upload(path, remote, [stage], 0)
    assert path.exists()
    assert not list(remote.glob("*.manifest"))


def test_corrupt_remote_readback_keeps_dump(sandbox, monkeypatch):
    stage, remote = sandbox
    path = make_dump(stage)
    verify = backup.verify_part_list

    def corrupt_then_verify(destination, parts, size, sha):
        (destination / parts[0][0]).write_bytes(b"corrupted")
        verify(destination, parts, size, sha)

    monkeypatch.setattr(backup, "verify_part_list", corrupt_then_verify)
    with pytest.raises(backup.BackupError, match="checksum mismatch"):
        backup.upload(path, remote, [stage], 0)
    assert path.exists()
    assert not list(remote.glob("*.manifest"))


def test_completed_upload_crash_before_unlink_can_resume(sandbox, monkeypatch):
    stage, remote = sandbox
    path = make_dump(stage)
    unlink = Path.unlink

    def fail_local_unlink(self, *args, **kwargs):
        if self == path:
            raise OSError("simulated crash before local cleanup")
        return unlink(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_local_unlink)
        with pytest.raises(OSError, match="simulated crash"):
            backup.upload(path, remote, [stage], 0)
    assert path.exists()
    manifest = next(remote.glob("*.manifest"))
    before = manifest.stat().st_mtime_ns
    backup.upload(path, remote, [stage], 0)
    assert not path.exists()
    assert manifest.stat().st_mtime_ns == before  # Reused, not overwritten.


def test_corrupt_existing_completed_backup_does_not_delete_local(sandbox):
    stage, remote = sandbox
    path = make_dump(stage)
    content = path.read_bytes()
    manifest = backup.upload(path, remote, [stage], 0)
    path.write_bytes(content)
    part = remote / backup.read_manifest(manifest)[2][0][0]
    part.write_bytes(b"bad")
    with pytest.raises(backup.BackupError):
        backup.upload(path, remote, [stage], 0)
    assert path.exists()


def test_pending_retried_newest_first_never_exports(sandbox, monkeypatch):
    stage, remote = sandbox
    older = make_dump(stage, "20261024_031701")
    newer = make_dump(stage)
    calls = []
    upload = backup.upload

    def record_upload(path, *args):
        calls.append(path)
        return upload(path, *args)

    monkeypatch.setattr(backup, "upload", record_upload)
    monkeypatch.setattr(backup, "dump_new", lambda *_: pytest.fail("must retry before exporting"))
    backup.execute(stage, remote)
    assert calls == [newer, older]
    assert len(list(remote.glob("*.manifest"))) == 2


def test_failed_retry_does_not_prune_old_remote_or_local(sandbox, monkeypatch):
    stage, remote = sandbox
    old = make_dump(stage, "20260901_031701")
    manifest = backup.upload(old, remote, [stage], 0)
    os.utime(manifest, (1, 1))
    path = make_dump(stage)
    monkeypatch.setattr(backup, "upload", lambda *_: (_ for _ in ()).throw(OSError("network down")))
    with pytest.raises(OSError):
        backup.execute(stage, remote)
    assert path.exists() and manifest.exists()


def test_prune_only_completed_sets_after_success(sandbox):
    stage, remote = sandbox
    old = backup.upload(make_dump(stage, "20260901_031701"), remote, [stage], 0)
    old_parts = backup.read_manifest(old)[2]
    os.utime(old, (1, 1))
    orphan = remote / "stock_data_20260902_031701.dump.part-aa"
    orphan.write_bytes(b"incomplete upload")
    unrelated = remote / "full_market_snapshot.dump"
    unrelated.write_bytes(b"keep")
    backup.execute(stage, remote, retry_only=True)
    assert old.exists()  # No new success, no deletion.
    make_dump(stage)
    backup.execute(stage, remote)
    assert not old.exists()
    assert all(not (remote / name).exists() for name, _, _ in old_parts)
    assert orphan.exists() and unrelated.exists()


def test_low_space_stops_before_pg_dump_or_cleanup(sandbox, monkeypatch):
    stage, remote = sandbox
    monkeypatch.setattr(backup, "space_available", lambda _: 100)
    monkeypatch.setattr(backup, "run", lambda *_a, **_k: pytest.fail("pg_dump must not start"))
    with pytest.raises(backup.BackupError, match="insufficient space"):
        backup.execute(stage, remote)
    assert not list(stage.glob("*.dump*"))


def test_partial_dump_stops_new_exports_and_is_preserved(sandbox, monkeypatch):
    stage, remote = sandbox
    partial = stage / "stock_backup_20261028_031701.dump.partial"
    partial.write_bytes(b"unfinished")
    monkeypatch.setattr(backup, "dump_new", lambda *_: pytest.fail("must not export"))
    with pytest.raises(backup.BackupError, match="unfinished dump"):
        backup.execute(stage, remote)
    assert partial.read_bytes() == b"unfinished"


def test_missing_cos_mount_does_not_write_to_local_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "run", lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout="/ xfs\n"))
    with pytest.raises(backup.BackupError, match="not a mounted COS"):
        backup.cos_cache_policy(tmp_path)


def test_decoder_failure_preserves_dump_and_remote(sandbox, monkeypatch):
    stage, remote = sandbox
    path = make_dump(stage)
    monkeypatch.setattr(backup, "run", lambda *_a, **_k: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "pg_restore")))
    with pytest.raises(subprocess.CalledProcessError):
        backup.upload(path, remote, [stage], 0)
    assert path.exists() and not list(remote.iterdir())


def _hold_lock(stage, ready):
    with backup.worker_lock(stage):
        ready.send(True)
        ready.recv()


def test_real_process_lock_excludes_manual_and_cron(tmp_path):
    ctx = multiprocessing.get_context("fork")
    parent, child = ctx.Pipe()
    worker = ctx.Process(target=_hold_lock, args=(tmp_path, child))
    worker.start()
    try:
        assert parent.poll(5) and parent.recv()
        with pytest.raises(backup.BackupError, match="another backup"):
            with backup.worker_lock(tmp_path):
                pytest.fail("concurrent worker entered")
    finally:
        parent.send(True)
        worker.join(5)
        if worker.is_alive():
            worker.kill()
            worker.join()
    with backup.worker_lock(tmp_path):
        pass


def test_manifest_path_traversal_is_rejected(sandbox):
    _, remote = sandbox
    manifest = remote / "stock_data_20260901_031701.dump.manifest"
    manifest.write_text(f"full_sha256: {'0' * 64}\nfull_size_bytes: 1\nparts:\n  ../victim 1 {'0' * 64}\n")
    with pytest.raises(backup.BackupError, match="invalid part reference"):
        backup.read_manifest(manifest)


def test_failed_export_is_partial_and_never_uploaded(sandbox, monkeypatch):
    stage, remote = sandbox
    monkeypatch.setattr(backup, "space_available", lambda _: 20 * 1024 * backup.MIB)
    monkeypatch.setenv("STOCK_DB_PASSWORD", "test-only")
    monkeypatch.setattr(backup, "upload", lambda *_: pytest.fail("failed export cannot be uploaded"))

    def fail_dump(argv, **kwargs):
        assert argv[0] == "pg_dump"
        assert callable(kwargs["preexec_fn"])  # File-size limit applied to child.
        Path(argv[-1]).write_bytes(b"incomplete export")
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(backup, "run", fail_dump)
    with pytest.raises(subprocess.CalledProcessError):
        backup.execute(stage, remote)
    assert len(list(stage.glob("*.dump.partial"))) == 1
    assert not list(stage.glob("*.dump"))


def test_no_pending_retry_only_never_exports(sandbox, monkeypatch):
    stage, remote = sandbox
    monkeypatch.setattr(backup, "dump_new", lambda *_: pytest.fail("retry-only must not export"))
    backup.execute(stage, remote, retry_only=True)


def test_full_decoder_is_used_not_just_toc(sandbox, monkeypatch):
    stage, _ = sandbox
    path = make_dump(stage)
    calls = []
    monkeypatch.setattr(backup, "run", lambda argv, **_: calls.append(argv))
    size, sha = backup.verify_dump(path)
    assert calls == [["pg_restore", "--file=/dev/null", str(path)]]
    assert size == path.stat().st_size and sha == backup.digest(path)


@pytest.mark.parametrize("use_cache", [False, True])
def test_cos_policy_checks_legacy_tmp_space_even_with_other_tmpdir(tmp_path, monkeypatch, use_cache):
    destination = tmp_path / "remote"
    destination.mkdir()
    proc = tmp_path / "proc"
    (proc / "123").mkdir(parents=True)
    args = ["/usr/local/bin/cosfs", "bucket", str(destination),
            "-otmpdir=/cloud/temp", "-oensure_diskfree=1024"]
    if use_cache:
        args.append("-ouse_cache=/cloud/cache")
    (proc / "123/cmdline").write_bytes(("\0".join(args) + "\0").encode())
    monkeypatch.setattr(backup, "Path", lambda name: proc if name == "/proc" else Path(name))
    monkeypatch.setattr(backup, "run", lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout=f"{destination} fuse.cosfs\n"))
    paths, reserve = backup.cos_cache_policy(destination)
    assert paths == [Path("/cloud/temp"), Path("/cloud/cache" if use_cache else "/tmp")]
    assert reserve == 1024 * backup.MIB


@pytest.mark.parametrize("mount_exists", [False, True])
def test_sec_archive_rejects_missing_or_wrong_mount_before_any_copy(tmp_path, mount_exists):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "unexpected_mutation"
    commands = {"mountpoint": "exit 0" if mount_exists else "exit 1",
                "findmnt": "echo xfs", "find": "exit 0"}
    for name in ("mkdir", "cp", "rm"):
        commands[name] = f"touch '{marker}'; exit 1"
    for name, body in commands.items():
        script = bin_dir / name
        script.write_text("#!/bin/sh\n" + body + "\n")
        script.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / "scripts/archive_sec_cache.sh"
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                            env={**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert result.returncode == 1
    assert "保留全部本地 SEC 缓存" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize("corrupt_copy", [False, True])
def test_sec_archive_checks_content_before_removing_source(tmp_path, corrupt_copy):
    project = tmp_path / "project"
    scripts = project / "scripts"
    cache = project / "data/sec_cache"
    remote = tmp_path / "remote"
    bin_dir = tmp_path / "bin"
    for p in [scripts, cache, remote, bin_dir]:
        p.mkdir(parents=True)
    original = Path(__file__).resolve().parents[1] / "scripts/archive_sec_cache.sh"
    script = scripts / "archive_sec_cache.sh"
    script.write_text(original.read_text().replace('COS_ROOT="/lhcos-data/sec_cache_archive"', f'COS_ROOT="{remote}"'))
    payload = b"original cache payload"
    source = cache / "ABC.json"
    source.write_bytes(payload)
    os.utime(source, (1, 1))
    # The index fallback is always retained, even when old.
    index = cache / "russell1000_tickers.json"
    index.write_bytes(b"index")
    os.utime(index, (1, 1))
    commands = {"mountpoint": "exit 0", "findmnt": "echo fuse.cosfs"}
    if corrupt_copy:
        commands["cp"] = '/usr/bin/cp "$@"\nprintf X | /usr/bin/dd of="$2" bs=1 count=1 conv=notrunc status=none'
    for name, body in commands.items():
        executable = bin_dir / name
        executable.write_text("#!/bin/sh\n" + body + "\n")
        executable.chmod(0o755)
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                            env={**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert result.returncode == int(corrupt_copy), result.stderr
    if corrupt_copy:
        assert source.read_bytes() == payload
        assert not list(remote.glob("*/ABC.json"))
        assert "校验不一致" in result.stdout
    else:
        assert not source.exists()
        assert next(remote.glob("*/ABC.json")).read_bytes() == payload
    assert index.read_bytes() == b"index"


def test_new_export_uses_last_archive_size_and_caps_shared_disk(sandbox, monkeypatch):
    stage, _ = sandbox
    free = 6000 * backup.MIB
    reserve = 3000 * backup.MIB
    monkeypatch.setattr(backup, "space_available", lambda _: free)
    monkeypatch.setenv("STOCK_DB_PASSWORD", "test-only")
    monkeypatch.delenv("STOCK_BACKUP_ESTIMATED_DUMP_MIB", raising=False)
    limits = []
    monkeypatch.setattr(backup.resource, "setrlimit", lambda _kind, values: limits.append(values))

    def fake_run(argv, **kwargs):
        if argv[0] == "pg_dump":
            kwargs["preexec_fn"]()
            Path(argv[-1]).write_bytes(b"valid test dump")

    monkeypatch.setattr(backup, "run", fake_run)
    # A blanket 3 GiB estimate would refuse this disk; the known 1 GiB dump fits.
    with pytest.raises(backup.BackupError, match="insufficient space"):
        backup.dump_new(stage, [stage], reserve)
    complete = backup.dump_new(stage, [stage], reserve, previous_size=1000 * backup.MIB)
    assert complete.exists()
    assert limits == [(free - reserve - 2 * backup.PART_BYTES,) * 2]


def test_execute_passes_latest_completed_size_to_export(sandbox, monkeypatch):
    stage, remote = sandbox
    old = make_dump(stage, "20261026_031701")
    previous_size = old.stat().st_size
    backup.upload(old, remote, [stage], 0)
    seen = []

    def fake_dump(stage_dir, paths, reserve, size):
        seen.append(size)
        return make_dump(stage_dir)

    monkeypatch.setattr(backup, "dump_new", fake_dump)
    backup.execute(stage, remote)
    assert seen == [previous_size]
