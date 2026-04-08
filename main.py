#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class CIFSMountError(Exception):
    pass


@dataclass(frozen=True)
class CIFSConfig:
    share: str
    mount_point: Path
    username: Optional[str] = None
    password: Optional[str] = None
    domain: Optional[str] = None
    credentials_file: Optional[Path] = None
    uid: Optional[int] = None
    gid: Optional[int] = None
    file_mode: Optional[str] = None
    dir_mode: Optional[str] = None
    vers: Optional[str] = None
    read_only: bool = False
    extra_options: Optional[str] = None


class CIFSManager:
    def __init__(self) -> None:
        self.mount_binary = shutil.which("mount.cifs")
        self.umount_binary = shutil.which("umount")
        if not self.mount_binary:
            raise CIFSMountError("mount.cifs not found in PATH. Install cifs-utils.")
        if not self.umount_binary:
            raise CIFSMountError("umount not found in PATH.")

    def ensure_mount_point(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def is_mounted(self, mount_point: Path) -> bool:
        mount_point = mount_point.resolve()
        try:
            with open("/proc/mounts", "r", encoding="utf-8") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 2 and Path(parts[1]).resolve() == mount_point:
                        return True
        except OSError as exc:
            raise CIFSMountError(f"failed to read /proc/mounts: {exc}") from exc
        return False

    def build_options(self, config: CIFSConfig) -> str:
        opts: list[str] = []

        if config.credentials_file:
            opts.append(f"credentials={config.credentials_file}")
        else:
            if config.username is not None:
                opts.append(f"username={config.username}")
            if config.password is not None:
                opts.append(f"password={config.password}")
            if config.domain is not None:
                opts.append(f"domain={config.domain}")

        if config.uid is not None:
            opts.append(f"uid={config.uid}")
        if config.gid is not None:
            opts.append(f"gid={config.gid}")
        if config.file_mode is not None:
            opts.append(f"file_mode={config.file_mode}")
        if config.dir_mode is not None:
            opts.append(f"dir_mode={config.dir_mode}")
        if config.vers is not None:
            opts.append(f"vers={config.vers}")
        if config.read_only:
            opts.append("ro")

        if config.extra_options:
            extra = [item.strip() for item in config.extra_options.split(",") if item.strip()]
            opts.extend(extra)

        return ",".join(opts)

    def validate(self, config: CIFSConfig) -> None:
        if not config.share.startswith("//"):
            raise CIFSMountError("share must look like //server/share")

        if config.credentials_file and (config.username or config.password or config.domain):
            raise CIFSMountError("use either credentials_file or username/password/domain, not both")

        if not config.credentials_file and not config.username:
            raise CIFSMountError("username is required unless credentials_file is used")

        if config.credentials_file:
            if not config.credentials_file.exists():
                raise CIFSMountError(f"credentials file does not exist: {config.credentials_file}")
            if not config.credentials_file.is_file():
                raise CIFSMountError(f"credentials path is not a file: {config.credentials_file}")

    def mount(self, config: CIFSConfig) -> None:
        self.validate(config)
        self.ensure_mount_point(config.mount_point)

        if self.is_mounted(config.mount_point):
            raise CIFSMountError(f"mount point is already mounted: {config.mount_point}")

        options = self.build_options(config)
        cmd = [
            self.mount_binary,
            config.share,
            str(config.mount_point),
        ]

        if options:
            cmd.extend(["-o", options])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip() or "unknown mount error"
            raise CIFSMountError(stderr)

    def unmount(self, mount_point: Path) -> None:
        if not self.is_mounted(mount_point):
            raise CIFSMountError(f"mount point is not mounted: {mount_point}")

        result = subprocess.run(
            [self.umount_binary, str(mount_point)],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip() or "unknown unmount error"
            raise CIFSMountError(stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="cifs-manager")

    subparsers = parser.add_subparsers(dest="command", required=True)

    mount_parser = subparsers.add_parser("mount")
    mount_parser.add_argument("share")
    mount_parser.add_argument("mount_point")
    mount_parser.add_argument("--username")
    mount_parser.add_argument("--password")
    mount_parser.add_argument("--domain")
    mount_parser.add_argument("--credentials-file")
    mount_parser.add_argument("--uid", type=int)
    mount_parser.add_argument("--gid", type=int)
    mount_parser.add_argument("--file-mode")
    mount_parser.add_argument("--dir-mode")
    mount_parser.add_argument("--vers")
    mount_parser.add_argument("--read-only", action="store_true")
    mount_parser.add_argument("--extra-options")

    unmount_parser = subparsers.add_parser("unmount")
    unmount_parser.add_argument("mount_point")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("mount_point")

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        manager = CIFSManager()

        if args.command == "mount":
            config = CIFSConfig(
                share=args.share,
                mount_point=Path(args.mount_point),
                username=args.username,
                password=args.password,
                domain=args.domain,
                credentials_file=Path(args.credentials_file) if args.credentials_file else None,
                uid=args.uid,
                gid=args.gid,
                file_mode=args.file_mode,
                dir_mode=args.dir_mode,
                vers=args.vers,
                read_only=args.read_only,
                extra_options=args.extra_options,
            )
            manager.mount(config)
            print(f"mounted {config.share} on {config.mount_point}")
            return 0

        if args.command == "unmount":
            mount_point = Path(args.mount_point)
            manager.unmount(mount_point)
            print(f"unmounted {mount_point}")
            return 0

        if args.command == "status":
            mount_point = Path(args.mount_point)
            print("mounted" if manager.is_mounted(mount_point) else "not mounted")
            return 0

        return 1

    except CIFSMountError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"os error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
