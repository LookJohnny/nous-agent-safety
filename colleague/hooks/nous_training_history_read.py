"""Stable bounded history reads, including optional stdlib Zstandard support.

Raw and expanded transcript material stays in memory. No shell, files written,
third-party decompressor, model calls or filesystem fallback are used.
"""
import contextlib
import hashlib
import os
import stat

from nous_transcript import MAX_BYTES, MAX_LINE_BYTES, MAX_LINES, TranscriptReadError, _check_bytes

try:
    from compression import zstd as _zstd
except ImportError:
    _zstd = None

MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_FRAMES = 10_000
WINDOW_LOG_MAX = 27
CHUNK_SIZE = 64 * 1024
ERROR_CODES = frozenset({
    "history_candidate_invalid", "history_source_unreadable", "history_secure_open_unavailable",
    "history_symlink_rejected", "history_regular_file_required", "history_source_changed",
    "history_source_too_large", "history_compression_unsupported", "history_compression_invalid",
    "history_frame_limit", "transcript_too_large", "transcript_line_too_large",
    "transcript_too_many_lines",
})


class HistoryReadError(ValueError):
    """Static error codes only; never include source strings or decompressor text."""


def _signature(info):
    return {"dev": info.st_dev, "ino": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def _open_dir(path):
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise HistoryReadError("history_secure_open_unavailable")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(os.path.sep, flags)
    try:
        for part in path.split(os.path.sep):
            if not part:
                continue
            before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise HistoryReadError("history_symlink_rejected")
            if not stat.S_ISDIR(before.st_mode):
                raise HistoryReadError("history_source_unreadable")
            next_fd = os.open(part, flags, dir_fd=fd)
            after = os.fstat(next_fd)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                os.close(next_fd)
                raise HistoryReadError("history_source_changed")
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _validate(path, expected, compression):
    if (not isinstance(path, str) or not path or "\0" in path
            or not os.path.isabs(path) or os.path.normpath(path) != path
            or compression not in {"none", "zstd"}
            or not isinstance(expected, dict)
            or set(expected) != {"dev", "ino", "size", "mtime_ns", "ctime_ns"}
            or any(type(value) is not int for value in expected.values())
            or any(expected[key] < 0 for key in ("dev", "ino", "size"))):
        raise HistoryReadError("history_candidate_invalid")
    if compression == "zstd" and _zstd is None:
        raise HistoryReadError("history_compression_unsupported")


@contextlib.contextmanager
def _source(path, expected):
    parent, name = os.path.split(path)
    directory = fd = None
    try:
        directory = _open_dir(parent)
        parent_info = os.fstat(directory)
        before = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            raise HistoryReadError("history_symlink_rejected")
        if not stat.S_ISREG(before.st_mode):
            raise HistoryReadError("history_regular_file_required")
        if _signature(before) != expected:
            raise HistoryReadError("history_source_changed")
        if before.st_size > MAX_SOURCE_BYTES:
            raise HistoryReadError("history_source_too_large")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_CLOEXEC", 0), dir_fd=directory)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _signature(opened) != expected:
            raise HistoryReadError("history_source_changed")
        with os.fdopen(fd, "rb") as handle:
            fd = None
            yield handle
            after = os.fstat(handle.fileno())
        leaf = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(leaf.st_mode) or _signature(after) != expected or _signature(leaf) != expected:
            raise HistoryReadError("history_source_changed")
        check_fd = _open_dir(parent)
        try:
            final_parent = os.fstat(check_fd)
            if (parent_info.st_dev, parent_info.st_ino) != (final_parent.st_dev, final_parent.st_ino):
                raise HistoryReadError("history_source_changed")
        finally:
            os.close(check_fd)
    except HistoryReadError:
        raise
    except (OSError, ValueError, TypeError, OverflowError):
        raise HistoryReadError("history_source_unreadable") from None
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)


def _raw_chunks(handle, expected_size, state):
    remaining = expected_size
    while remaining:
        chunk = handle.read(min(CHUNK_SIZE, remaining))
        if not chunk:
            raise HistoryReadError("history_source_changed")
        remaining -= len(chunk)
        state["bytes"] += len(chunk)
        state["hash"].update(chunk)
        yield chunk
    if handle.read(1):
        raise HistoryReadError("history_source_changed")


def _expanded_chunks(raw, compression):
    if compression == "none":
        yield from raw
        return
    decoder, frames = None, 0
    try:
        for chunk in raw:
            pending = chunk
            while pending or (decoder is not None and not decoder.needs_input):
                if decoder is None:
                    if frames >= MAX_FRAMES:
                        raise HistoryReadError("history_frame_limit")
                    decoder = _zstd.ZstdDecompressor(options={_zstd.DecompressionParameter.window_log_max: WINDOW_LOG_MAX})
                output = decoder.decompress(pending, max_length=CHUNK_SIZE)
                pending = b""
                if decoder.eof:
                    frames += 1
                    pending = decoder.unused_data
                    decoder = None
                if output:
                    yield output
        if decoder is not None or frames == 0:
            raise HistoryReadError("history_compression_invalid")
    except HistoryReadError:
        raise
    except (ValueError, TypeError, EOFError, _zstd.ZstdError):
        raise HistoryReadError("history_compression_invalid") from None


def _header_prefix(chunks, max_bytes, max_lines):
    data = bytearray()
    line_count = 0
    complete = True
    limit = None
    for chunk in chunks:
        take = min(len(chunk), max_bytes - len(data))
        piece = chunk[:take]
        remaining_lines = max_lines - line_count
        start = 0
        for _ in range(remaining_lines):
            newline = piece.find(b"\n", start)
            if newline < 0:
                break
            line_count += 1
            start = newline + 1
            if line_count == max_lines:
                piece = piece[:start]
                break
        data.extend(piece)
        if line_count >= max_lines or len(data) >= max_bytes:
            complete = False  # A prefix at the budget is not a full-frame claim.
            limit = "lines" if line_count >= max_lines else "bytes"
            break
    if complete and data and not data.endswith(b"\n"):
        line_count += 1
    # An incomplete final physical line is not passed to the metadata parser.
    safe = data if complete or data.endswith(b"\n") else data[:data.rfind(b"\n") + 1]
    return bytes(safe), {"bytes_read": len(data), "lines_read": line_count,
                         "complete": complete, "limit": limit}


def read_history_header(path, expected, compression="none", *, max_bytes=1024 * 1024, max_lines=128):
    """Internal scanner API: decode only a bounded prefix, never promise a full frame."""
    _validate(path, expected, compression)
    if type(max_bytes) is not int or not 1 <= max_bytes <= 1024 * 1024 or type(max_lines) is not int or not 1 <= max_lines <= 128:
        raise HistoryReadError("history_candidate_invalid")
    state = {"bytes": 0, "hash": hashlib.sha256()}
    with _source(path, expected) as handle:
        snapshot, header = _header_prefix(_expanded_chunks(_raw_chunks(handle, expected["size"], state), compression), max_bytes, max_lines)
    header["source_bytes_read"] = state["bytes"]
    return {"snapshot": snapshot, "header": header, "stat": dict(expected)}


def read_history_bytes(candidate):
    """Read the exact candidate file once; hash raw and expanded bytes separately."""
    if not isinstance(candidate, dict):
        raise HistoryReadError("history_candidate_invalid")
    path, expected, compression = candidate.get("path"), candidate.get("stat"), candidate.get("compression", "none")
    _validate(path, expected, compression)
    if candidate.get("source_id") != hashlib.sha256(os.fsencode(path)).hexdigest():
        raise HistoryReadError("history_candidate_invalid")
    state = {"bytes": 0, "hash": hashlib.sha256()}
    data = bytearray()
    lines = trailing = 0
    with _source(path, expected) as handle:
        for chunk in _expanded_chunks(_raw_chunks(handle, expected["size"], state), compression):
            if len(data) + len(chunk) > MAX_BYTES:
                raise HistoryReadError("transcript_too_large")
            first = chunk.find(b"\n")
            if first < 0:
                trailing += len(chunk)
            else:
                if trailing + first + 1 > MAX_LINE_BYTES:
                    raise HistoryReadError("transcript_line_too_large")
                segments = chunk.split(b"\n")
                if any(len(piece) + 1 > MAX_LINE_BYTES for piece in segments[1:-1]):
                    raise HistoryReadError("transcript_line_too_large")
                trailing = len(segments[-1])
                lines += len(segments) - 1
            if trailing > MAX_LINE_BYTES:
                raise HistoryReadError("transcript_line_too_large")
            if lines + int(trailing > 0) > MAX_LINES:
                raise HistoryReadError("transcript_too_many_lines")
            data.extend(chunk)
    snapshot = bytes(data)
    try:
        _check_bytes(snapshot, max_bytes=MAX_BYTES, max_line_bytes=MAX_LINE_BYTES)
    except TranscriptReadError as exc:
        raise HistoryReadError(str(exc)) from None
    return {"snapshot": snapshot, "source_sha256": state["hash"].hexdigest(),
            "snapshot_sha256": hashlib.sha256(snapshot).hexdigest()}
