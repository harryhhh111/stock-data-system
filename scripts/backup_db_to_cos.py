#!/usr/bin/env python3
"""Bounded full backups: retry first, verify uploaded bytes, then remove staging.

Uses only the standard library and PostgreSQL CLI tools. Never deletes an
unarchived dump, including failed/partial dumps. COS must be genuinely mounted.
"""
import argparse
import contextlib
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys
import time

MIB = 1024 * 1024
PART_BYTES = int(os.environ.get("STOCK_BACKUP_PART_MIB", "64")) * MIB
DUMP_RE = re.compile(r"stock_backup_(\d{8}_\d{6})\.dump\Z")
MANIFEST_RE = re.compile(r"stock_data_\d{8}_\d{6}\.dump\.manifest\Z")


class BackupError(RuntimeError):
    pass


def log(message):
    print(f"[{datetime.now():%F %T}] {message}", flush=True)


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, timeout=3600, **kwargs)


def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(MIB), b""):
            sha.update(block)
    return sha.hexdigest()


@contextlib.contextmanager
def worker_lock(stage_dir):
    # Separate from cron's outer lock: manual calls and cron share this lock.
    fd = os.open(stage_dir / ".stock-backup-worker.lock",
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("another backup worker is running") from exc
        yield
    finally:
        os.close(fd)


def space_available(path):
    # A root-owned cosfs tmpdir may not be searchable by the backup user.
    # Its parent must be on the same filesystem; verify using st_dev.
    try:
        return shutil.disk_usage(path).free
    except PermissionError:
        if path.stat().st_dev != path.parent.stat().st_dev:
            raise BackupError(f"cannot inspect cache filesystem: {path}")
        return shutil.disk_usage(path.parent).free


def require_space(path, required):
    free = space_available(path)
    if free < required:
        raise BackupError(f"insufficient space on {path}: free={free}, required={required}; "
                          "no new dump will be created; existing backups preserved")


def cos_cache_policy(destination):
    """Inspect the actual host mount/process, not just an existing directory."""
    try:
        mount = run(["findmnt", "-n", "-o", "TARGET,FSTYPE", "-T", str(destination)],
                    capture_output=True, text=True).stdout.split()
    except subprocess.CalledProcessError as exc:
        raise BackupError(f"COS directory/mount unavailable: {destination}") from exc
    if len(mount) != 2 or mount[1] != "fuse.cosfs" or not destination.is_dir():
        raise BackupError(f"not a mounted COS directory: {destination}")
    for proc in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = proc.read_bytes().decode().strip("\0").split("\0")
        except (OSError, UnicodeError):
            continue
        if not args or Path(args[0]).name != "cosfs" or mount[0] not in args:
            continue
        options = {}
        for index, arg in enumerate(args):
            value = args[index + 1] if arg == "-o" and index + 1 < len(args) else arg[2:] if arg.startswith("-o") else ""
            for entry in value.split(","):
                key, _, val = entry.partition("=")
                options[key] = val
        if "ensure_diskfree" not in options:
            raise BackupError("cosfs ensure_diskfree must be explicit before backup")
        reserve = max(1024, int(options["ensure_diskfree"])) * MIB
        paths = [Path(options.get("tmpdir", "/tmp"))]
        if options.get("use_cache"):
            paths.append(Path(options["use_cache"]))
        else:
            # Local cosfs 1.0.21 checks literal /tmp in GetFreeDiskSpace when
            # use_cache is unset, even if temporary I/O uses another tmpdir.
            # Conservatively check both rather than trusting tmpdir alone.
            if Path("/tmp") not in paths:
                paths.append(Path("/tmp"))
        return paths, reserve
    raise BackupError("cannot inspect cosfs process/cache policy; run on the host")


def check_upload_space(cache_paths, reserve):
    for path in cache_paths:
        require_space(path, reserve + 2 * PART_BYTES)


def verify_dump(path):
    if path.is_symlink() or not path.is_file() or not path.stat().st_size:
        raise BackupError(f"invalid staging file: {path}")
    # --list alone does not validate compressed table-data blocks.
    run(["pg_restore", "--file=/dev/null", str(path)], stdout=subprocess.DEVNULL)
    return path.stat().st_size, digest(path)


def read_manifest(path):
    lines = path.read_text().splitlines()
    if len(lines) < 4 or not lines[0].startswith("full_sha256: ") or not lines[1].startswith("full_size_bytes: ") or lines[2] != "parts:":
        raise BackupError(f"invalid manifest: {path}")
    sha, size = lines[0].split()[1], int(lines[1].split()[1])
    if not re.fullmatch(r"[0-9a-f]{64}", sha) or size <= 0:
        raise BackupError(f"invalid manifest checksum/size: {path}")
    parts = []
    prefix = path.name.removesuffix(".manifest") + ".part-"
    for line in lines[3:]:
        if not line.startswith("  "):
            break
        name, count, checksum = line.split()
        if not re.fullmatch(re.escape(prefix) + r"[a-z]{2}", name) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise BackupError(f"invalid part reference: {path}")
        parts.append((name, int(count), checksum))
    expected = [prefix + part_suffix(i) for i in range(len(parts))]
    if not parts or [p[0] for p in parts] != expected or any(p[1] <= 0 for p in parts) or sum(p[1] for p in parts) != size:
        raise BackupError(f"invalid part sequence/size: {path}")
    return size, sha, parts


def part_suffix(index):
    if index >= 26 * 26:
        raise BackupError("too many backup parts")
    return chr(97 + index // 26) + chr(97 + index % 26)


def verify_remote(manifest, expected_size, expected_sha):
    size, sha, parts = read_manifest(manifest)
    if (size, sha) != (expected_size, expected_sha):
        raise BackupError(f"existing completed backup conflicts with local dump: {manifest}")
    verify_part_list(manifest.parent, parts, size, sha)


def upload(path, destination, cache_paths, reserve):
    check_upload_space(cache_paths, reserve)
    log(f"validating staging dump before upload: {path.name}")
    before = path.stat()
    size, sha = verify_dump(path)
    timestamp = DUMP_RE.fullmatch(path.name).group(1)
    name = f"stock_data_{timestamp}.dump"
    manifest = destination / (name + ".manifest")
    if not manifest.exists():
        parts = []
        with path.open("rb") as src:
            remaining = size
            while remaining:
                check_upload_space(cache_paths, reserve)
                part = destination / (name + ".part-" + part_suffix(len(parts)))
                count = min(PART_BYTES, remaining)
                checksum = hashlib.sha256()
                with part.open("wb") as dst:
                    left = count
                    while left:
                        block = src.read(min(MIB, left))
                        if not block:
                            raise BackupError(f"staging file truncated during upload: {path}")
                        dst.write(block)
                        checksum.update(block)
                        left -= len(block)
                parts.append((part.name, count, checksum.hexdigest()))
                remaining -= count
                log(f"uploaded part: {part.name}")
        contents = f"full_sha256: {sha}\nfull_size_bytes: {size}\nparts:\n"
        contents += "".join(f"  {n} {s} {h}\n" for n, s, h in parts)
        contents += f"assemble: cat {name}.part-* > {name}\n"
        provisional = destination / (name + ".manifest.uploading")
        provisional.write_text(contents)
        # Verification uses the final-name grammar, but nothing is published as
        # completed until every uploaded part has been read back successfully.
        verify_part_list(destination, parts, size, sha)
        provisional.replace(manifest)
        if read_manifest(manifest) != (size, sha, parts):
            raise BackupError(f"published manifest differs: {manifest}")
    else:
        verify_remote(manifest, size, sha)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise BackupError(f"staging file changed during upload: {path}; preserved")
    path.unlink()
    log(f"verified and archived: {name}, bytes={size}, sha256={sha}; local dump removed")
    return manifest


def verify_part_list(destination, parts, size, sha):
    full = hashlib.sha256()
    total = 0
    for name, count, expected in parts:
        actual = hashlib.sha256()
        part_size = 0
        with (destination / name).open("rb") as stream:
            for block in iter(lambda: stream.read(MIB), b""):
                full.update(block)
                actual.update(block)
                total += len(block)
                part_size += len(block)
        if part_size != count or actual.hexdigest() != expected:
            raise BackupError(f"uploaded part checksum mismatch: {name}")
    if total != size or full.hexdigest() != sha:
        raise BackupError("uploaded full checksum mismatch")


def prune_remote(destination, keep, retention_days):
    # Only known, completed backup sets; never orphan parts or ad-hoc snapshots.
    cutoff = time.time() - retention_days * 86400
    for manifest in sorted(destination.glob("*.manifest")):
        if manifest == keep or not MANIFEST_RE.fullmatch(manifest.name) or manifest.stat().st_mtime >= cutoff:
            continue
        _, _, parts = read_manifest(manifest)
        # Remove the completion marker first so an interrupted prune cannot
        # advertise a partially deleted backup as complete.
        manifest.unlink()
        for name, _, _ in parts:
            (destination / name).unlink(missing_ok=True)
        log(f"pruned expired completed backup: {manifest.name}")


def dump_new(stage_dir, cache_paths, reserve, previous_size=None):
    disk_reserve = 2048 * MIB
    estimate_setting = os.environ.get("STOCK_BACKUP_ESTIMATED_DUMP_MIB")
    estimate = (int(estimate_setting) * MIB if estimate_setting is not None
                else previous_size + 64 * MIB if previous_size is not None else 3072 * MIB)
    limit = int(os.environ.get("STOCK_BACKUP_MAX_LOCAL_MIB", "8192")) * MIB
    if estimate <= 0 or limit < estimate:
        raise BackupError("invalid backup size estimate/local limit")
    require_space(stage_dir, disk_reserve + estimate)
    check_upload_space(cache_paths, reserve)
    # If dump and COS temp files share a disk, reserve upload headroom as well.
    limit = min(limit, space_available(stage_dir) - disk_reserve)
    for path in cache_paths:
        if path.stat().st_dev == stage_dir.stat().st_dev:
            require_space(stage_dir, estimate + reserve + 2 * PART_BYTES)
            limit = min(limit, space_available(stage_dir) - reserve - 2 * PART_BYTES)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    complete = stage_dir / f"stock_backup_{timestamp}.dump"
    partial = stage_dir / (complete.name + ".partial")
    if complete.exists() or partial.exists():
        raise BackupError(f"backup name already exists: {complete}")
    env = dict(os.environ)
    password = env.get("STOCK_DB_PASSWORD")
    if not password:
        raise BackupError("STOCK_DB_PASSWORD is required")
    env["PGPASSWORD"] = password
    argv = ["pg_dump", "-h", env.get("STOCK_DB_HOST", "127.0.0.1"),
            "-p", env.get("STOCK_DB_PORT", "5432"), "-U", env.get("STOCK_DB_USER", "postgres"),
            "-d", env.get("STOCK_DB_NAME", "stock_data"), "-Fc", "--compress=6", "-f", str(partial)]

    def limit_file():
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

    log(f"dump start: {partial}; max_bytes={limit}")
    run(argv, env=env, preexec_fn=limit_file)
    verify_dump(partial)
    partial.replace(complete)
    return complete


def execute(stage_dir, destination, retry_only=False):
    cache_paths, reserve = cos_cache_policy(destination)
    check_upload_space(cache_paths, reserve)
    pending = sorted((p for p in stage_dir.iterdir() if DUMP_RE.fullmatch(p.name)), reverse=True)
    partials = list(stage_dir.glob("stock_backup_*.dump.partial"))
    newest_verified = None
    if pending:
        log(f"retrying {len(pending)} pending full backups, newest first; no new dump this run")
        for path in pending:
            archived = upload(path, destination, cache_paths, reserve)
            newest_verified = newest_verified or archived
    elif not partials and not retry_only:
        manifests = sorted((p for p in destination.glob("*.manifest") if MANIFEST_RE.fullmatch(p.name)), reverse=True)
        previous_size = read_manifest(manifests[0])[0] if manifests else None
        newest_verified = upload(dump_new(stage_dir, cache_paths, reserve, previous_size), destination, cache_paths, reserve)
    if newest_verified:
        prune_remote(destination, newest_verified, retention_days=14)
    if partials:
        raise BackupError("unfinished dump(s) require inspection; new dumps paused: " + ", ".join(p.name for p in partials))
    if retry_only and not pending:
        log("no pending complete backups; no database export requested")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retry-only", action="store_true", help="upload existing dumps only; never export DB")
    args = parser.parse_args()
    stage = Path("/tmp")
    destination = Path("/lhcos-data/database")
    status = Path(__file__).resolve().parents[1] / "logs/backup_db_to_cos_status.json"
    os.umask(0o077)
    try:
        with worker_lock(stage):
            record = {"at": datetime.now().astimezone().isoformat(), "status": "interrupted"}
            try:
                execute(stage, destination, retry_only=args.retry_only)
            except Exception as exc:
                record = {"at": datetime.now().astimezone().isoformat(), "status": "failed", "error": str(exc)}
                raise
            else:
                record = {"at": datetime.now().astimezone().isoformat(), "status": "success"}
            finally:
                status.parent.mkdir(exist_ok=True)
                temp = status.with_suffix(".tmp")
                temp.write_text(json.dumps(record, ensure_ascii=False) + "\n")
                temp.replace(status)
    except Exception as exc:
        log(f"ERROR: {exc}; no unarchived backups deleted")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
