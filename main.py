#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


class CIFSError(Exception):
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
    nofail: bool = False
    x_systemd_automount: bool = False


@dataclass(frozen=True)
class MountInfo:
    source: str
    mount_point: str
    fstype: str
    options: list[str]


class CIFSManager:
    def __init__(self) -> None:
        self.mount_binary = shutil.which("mount.cifs")
        self.umount_binary = shutil.which("umount")
        if not self.mount_binary:
            raise CIFSError("mount.cifs not found in PATH. Install cifs-utils.")
        if not self.umount_binary:
            raise CIFSError("umount not found in PATH.")

    def ensure_mount_point(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def _normalize_mount_path(self, path: str) -> str:
        return os.path.normpath(path.replace("\\040", " "))

    def _read_mounts(self) -> list[MountInfo]:
        mounts: list[MountInfo] = []
        try:
            with open("/proc/mounts", "r", encoding="utf-8") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    source = parts[0]
                    mount_point = self._normalize_mount_path(parts[1])
                    fstype = parts[2]
                    options = parts[3].split(",")
                    mounts.append(
                        MountInfo(
                            source=source,
                            mount_point=mount_point,
                            fstype=fstype,
                            options=options,
                        )
                    )
        except OSError as exc:
            raise CIFSError(f"failed to read /proc/mounts: {exc}") from exc
        return mounts

    def get_mount_info(self, mount_point: Path) -> Optional[MountInfo]:
        target = os.path.normpath(str(mount_point.resolve()))
        for item in self._read_mounts():
            if os.path.normpath(item.mount_point) == target:
                return item
        return None

    def is_mounted(self, mount_point: Path) -> bool:
        return self.get_mount_info(mount_point) is not None

    def is_cifs_mounted(self, mount_point: Path) -> bool:
        info = self.get_mount_info(mount_point)
        return info is not None and info.fstype.lower() == "cifs"

    def validate(self, config: CIFSConfig) -> None:
        if not config.share.startswith("//"):
            raise CIFSError("share must look like //server/share")

        if config.credentials_file and (config.username or config.password or config.domain):
            raise CIFSError("use either credentials_file or username/password/domain, not both")

        if not config.credentials_file and not config.username:
            raise CIFSError("username is required unless credentials_file is used")

        if config.credentials_file:
            if not config.credentials_file.exists():
                raise CIFSError(f"credentials file does not exist: {config.credentials_file}")
            if not config.credentials_file.is_file():
                raise CIFSError(f"credentials path is not a file: {config.credentials_file}")

    def _build_mount_options_list(self, config: CIFSConfig) -> list[str]:
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
        if config.nofail:
            opts.append("nofail")
        if config.x_systemd_automount:
            opts.append("x-systemd.automount")

        if config.extra_options:
            extra = [item.strip() for item in config.extra_options.split(",") if item.strip()]
            opts.extend(extra)

        return opts

    def build_options(self, config: CIFSConfig) -> str:
        return ",".join(self._build_mount_options_list(config))

    def mount(self, config: CIFSConfig) -> MountInfo:
        self.validate(config)
        self.ensure_mount_point(config.mount_point)

        if self.is_mounted(config.mount_point):
            raise CIFSError(f"mount point is already mounted: {config.mount_point}")

        options = self.build_options(config)
        cmd = [self.mount_binary, config.share, str(config.mount_point)]

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
            raise CIFSError(stderr)

        info = self.get_mount_info(config.mount_point)
        if info is None:
            raise CIFSError("mount command succeeded but mount was not found in /proc/mounts")

        if info.fstype.lower() != "cifs":
            raise CIFSError(
                f"mount point is mounted but filesystem type is {info.fstype}, expected cifs"
            )

        return info

    def unmount(self, mount_point: Path) -> None:
        info = self.get_mount_info(mount_point)
        if info is None:
            raise CIFSError(f"mount point is not mounted: {mount_point}")

        result = subprocess.run(
            [self.umount_binary, str(mount_point)],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip() or "unknown unmount error"
            raise CIFSError(stderr)

    def build_fstab_entry(self, config: CIFSConfig) -> str:
        self.validate(config)
        options = self._build_mount_options_list(config)
        return f"{config.share} {config.mount_point} cifs {','.join(options)} 0 0"


def mount_info_to_dict(info: Optional[MountInfo]) -> dict:
    if info is None:
        return {
            "mounted": False,
            "source": None,
            "mount_point": None,
            "fstype": None,
            "options": [],
        }
    return {
        "mounted": True,
        "source": info.source,
        "mount_point": info.mount_point,
        "fstype": info.fstype,
        "options": info.options,
    }


def print_json(data: dict) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def print_text_status(info: Optional[MountInfo]) -> None:
    if info is None:
        print("not mounted")
        return
    print(f"mounted      : yes")
    print(f"source       : {info.source}")
    print(f"mount_point  : {info.mount_point}")
    print(f"fstype       : {info.fstype}")
    print("options      :")
    for option in info.options:
        print(f"  {option}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cifs-manager")
    parser.add_argument("--json", action="store_true")

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
    mount_parser.add_argument("--nofail", action="store_true")
    mount_parser.add_argument("--x-systemd-automount", action="store_true")

    unmount_parser = subparsers.add_parser("unmount")
    unmount_parser.add_argument("mount_point")

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("mount_point")

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("mount_point")

    fstab_parser = subparsers.add_parser("fstab-entry")
    fstab_parser.add_argument("share")
    fstab_parser.add_argument("mount_point")
    fstab_parser.add_argument("--username")
    fstab_parser.add_argument("--password")
    fstab_parser.add_argument("--domain")
    fstab_parser.add_argument("--credentials-file")
    fstab_parser.add_argument("--uid", type=int)
    fstab_parser.add_argument("--gid", type=int)
    fstab_parser.add_argument("--file-mode")
    fstab_parser.add_argument("--dir-mode")
    fstab_parser.add_argument("--vers")
    fstab_parser.add_argument("--read-only", action="store_true")
    fstab_parser.add_argument("--extra-options")
    fstab_parser.add_argument("--nofail", action="store_true")
    fstab_parser.add_argument("--x-systemd-automount", action="store_true")

    return parser


def config_from_args(args: argparse.Namespace) -> CIFSConfig:
    return CIFSConfig(
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
        nofail=args.nofail,
        x_systemd_automount=args.x_systemd_automount,
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        manager = CIFSManager()

        if args.command == "mount":
            config = config_from_args(args)
            info = manager.mount(config)
            if args.json:
                print_json(
                    {
                        "ok": True,
                        "action": "mount",
                        "share": config.share,
                        "mount": mount_info_to_dict(info),
                    }
                )
            else:
                print(f"mounted {config.share} on {config.mount_point}")
                print_text_status(info)
            return 0

        if args.command == "unmount":
            mount_point = Path(args.mount_point)
            manager.unmount(mount_point)
            if args.json:
                print_json(
                    {
                        "ok": True,
                        "action": "unmount",
                        "mount_point": str(mount_point),
                    }
                )
            else:
                print(f"unmounted {mount_point}")
            return 0

        if args.command == "status":
            mount_point = Path(args.mount_point)
            info = manager.get_mount_info(mount_point)
            if args.json:
                print_json(
                    {
                        "ok": True,
                        "action": "status",
                        "mount": mount_info_to_dict(info),
                    }
                )
            else:
                print_text_status(info)
            return 0

        if args.command == "inspect":
            mount_point = Path(args.mount_point)
            info = manager.get_mount_info(mount_point)
            if args.json:
                print_json(
                    {
                        "ok": True,
                        "action": "inspect",
                        "mount": mount_info_to_dict(info),
                        "is_cifs": manager.is_cifs_mounted(mount_point),
                    }
                )
            else:
                print_text_status(info)
                print(f"is_cifs      : {'yes' if manager.is_cifs_mounted(mount_point) else 'no'}")
            return 0

        if args.command == "fstab-entry":
            config = config_from_args(args)
            entry = manager.build_fstab_entry(config)
            if args.json:
                print_json(
                    {
                        "ok": True,
                        "action": "fstab-entry",
                        "entry": entry,
                        "config": asdict(config),
                    }
                )
            else:
                print(entry)
            return 0

        return 1

    except CIFSError as exc:
        if "--json" in sys.argv[1:]:
            print_json({"ok": False, "error": str(exc)})
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        if "--json" in sys.argv[1:]:
            print_json({"ok": False, "error": f"os error: {exc}"})
        else:
            print(f"os error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
