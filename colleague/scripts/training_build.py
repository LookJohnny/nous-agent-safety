#!/usr/bin/env python3
"""Build reproducible, allowlisted developer-alpha archives; never publish them."""
import sys

if __name__ == "__main__" and not sys.flags.isolated:
    sys.stderr.write("nous-build: isolated_startup_required; use python -I -S\n")
    raise SystemExit(2)

import argparse
import gzip
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tarfile
import tempfile
import zipfile

sys.dont_write_bytecode = True
# Load one trusted sibling by exact path, without putting scripts/ on sys.path.
_spec = importlib.util.spec_from_file_location("nous_release_checker", Path(__file__).resolve().with_name("training_release_check.py"))
_checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_checker)
RELEASE_FILES, RUNTIME_FILES, TARGET_PYTHON = _checker.RELEASE_FILES, _checker.RUNTIME_FILES, _checker.TARGET_PYTHON
OPTIONAL_CAPABILITIES, isolated_env = _checker.OPTIONAL_CAPABILITIES, _checker.isolated_env


class BuildError(ValueError):
    pass


def _json(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _files(root):
    result = {}
    for name in sorted(RELEASE_FILES):
        path = root / name
        if not path.is_file() or path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root and root in p.parents):
            raise BuildError("required_source_missing_or_symlink")
        if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > 16 * 1024 * 1024:
            raise BuildError("invalid_release_source")
        result[name] = path.read_bytes()
    return result


def _unpack_tar(archive, destination, prefix):
    """Extract our generated files explicitly; never follow archive links."""
    with tarfile.open(archive, "r:gz") as stream:
        for member in stream.getmembers():
            name = PurePosixPath(member.name)
            if (not member.isfile() or name.is_absolute() or ".." in name.parts
                    or len(name.parts) < 2 or name.parts[0] != prefix):
                raise BuildError("unsafe_archive_member")
            target = destination.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with stream.extractfile(member) as source:
                target.write_bytes(source.read())


def build(source_root, out_dir, *, smoke=True, epoch=0):
    root, out = Path(source_root).resolve(), Path(out_dir).resolve()
    if type(epoch) is not int or not 0 <= epoch <= 0xFFFFFFFF:
        raise BuildError("invalid_source_date_epoch")
    files = _files(root)
    version = files["TRAINING_VERSION"].decode("utf-8").strip()
    if version != "0.1.0a9":
        raise BuildError("unsupported_training_release_version")
    prefix = "nous-training-" + version
    manifest = {"schema": "nous.training.release/1", "name": "nous-training", "version": version,
                "channel": "macos-developer-alpha", "published": False,
                "real_platform_acceptance": "not_completed", "windows_supported": False,
                "target_python_versions": list(TARGET_PYTHON), "external_python_dependencies": [],
                "optional_runtime_capabilities": OPTIONAL_CAPABILITIES,
                "runtime_files": list(RUNTIME_FILES), "source_date_epoch": epoch,
                "files": [{"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                          for name, raw in sorted(files.items())]}
    files["release-manifest.json"] = _json(manifest)
    out.mkdir(parents=True, exist_ok=False)
    tar_path, zip_path = out / (prefix + ".tar.gz"), out / (prefix + ".zip")
    with tar_path.open("xb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=epoch, compresslevel=9) as zipped:
            with tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for name, data in sorted(files.items()):
                    info = tarfile.TarInfo(prefix + "/" + name)
                    info.size, info.mode, info.mtime = len(data), 0o644, epoch
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    archive.addfile(info, io.BytesIO(data))
    # ZIP_STORED avoids compressor-version variability; source is small and no
    # third-party/binary assets belong in this minimal package.
    with zipfile.ZipFile(zip_path, "x", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(prefix + "/" + name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, data)
    (out / "release-manifest.json").write_bytes(files["release-manifest.json"])
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in (tar_path, zip_path, out / "release-manifest.json")}
    (out / "SHA256SUMS").write_text("".join(f"{sha}  {name}\n" for name, sha in sorted(hashes.items())), encoding="utf-8")
    report = {"schema": "nous.training.build-report/1", "version": version,
              "published": False, "archives": hashes, "file_count": len(files),
              "source_tests": "not_run_by_build_command", "smoke": {"status": "not_run"},
              "real_platform_acceptance": "not_run", "ci_matrix": "not_run_by_build_command"}
    if smoke:
        with tempfile.TemporaryDirectory(prefix="nous-clean-package-") as temporary:
            temp = Path(temporary)
            _unpack_tar(tar_path, temp, prefix)
            package = temp / prefix
            process = subprocess.run([sys.executable, "-I", "-S", "-B",
                                      str(package / "scripts/training_release_check.py"), "--smoke-only"],
                                     cwd=package, env=isolated_env(temp / "state"),
                                     capture_output=True, text=True, timeout=180)
            try:
                checked = json.loads(process.stdout)
            except ValueError:
                checked = {"passed": False, "error": "invalid_smoke_report"}
            report["smoke"] = {"status": "passed" if process.returncode == 0 and checked.get("passed") else "failed",
                               "report": checked}
    (out / "BUILD_REPORT.json").write_bytes(_json(report))
    if report["smoke"]["status"] == "failed":
        raise BuildError("extracted_package_smoke_failed")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path, required=True, help="new output directory; existing directories are refused")
    parser.add_argument("--no-smoke", action="store_true", help="build only; report explicitly records smoke not run")
    parser.add_argument("--source-date-epoch", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        report = build(args.source_root, args.out, smoke=not args.no_smoke, epoch=args.source_date_epoch)
    except (BuildError, OSError, subprocess.TimeoutExpired, ValueError) as error:
        code = str(error) if isinstance(error, BuildError) else type(error).__name__
        print(json.dumps({"error": "build_failed", "code": code}))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
