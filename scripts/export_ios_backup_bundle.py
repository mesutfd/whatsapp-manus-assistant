#!/usr/bin/env python3
"""
Build an importable "media bundle" zip from an iPhone (Finder/iTunes) backup.

An iOS ChatStorage.sqlite only references media by relative path
(Media/<jid>/x/y/<uuid>.<ext>); the actual files live in the device backup
under hashed names, resolvable through Manifest.db (domain
AppDomainGroup-group.net.whatsapp.WhatsApp.shared, relativePath
"Message/<local path>"). This script joins the two and writes a single zip:

    ChatStorage.sqlite          (at the root)
    Media/<jid>/x/y/<uuid>.jpg  (each referenced media file found)

Upload that zip to POST /api/v1/messages/import-backup — messages and media
are imported together (originals to GridFS, thumbnails generated for images).

Video files (.mp4/.mov) are excluded by default to keep the bundle small;
pass --include-videos to bundle them too. The backup must be UNENCRYPTED.

Example (personal WhatsApp):
    python3 scripts/export_ios_backup_bundle.py \
        --chatstorage ~/Desktop/whatsapp_backup/ChatStorage-WhatsApp.sqlite \
        --backup-dir ~/Library/Application\\ Support/MobileSync/Backup/<UDID> \
        --out wa-bundle.zip

For WhatsApp Business add: --domain AppDomainGroup-group.net.whatsapp.WhatsAppSMB.shared
"""

import argparse
import os
import sqlite3
import sys
import zipfile

VIDEO_EXTENSIONS = (".mp4", ".mov")
DEFAULT_DOMAIN = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"


def referenced_media_paths(chatstorage: str) -> list:
    con = sqlite3.connect(f"file:{os.path.abspath(chatstorage)}?mode=ro", uri=True)
    try:
        return [
            r[0]
            for r in con.execute(
                "SELECT ZMEDIALOCALPATH FROM ZWAMEDIAITEM WHERE ZMEDIALOCALPATH IS NOT NULL"
            )
        ]
    finally:
        con.close()


def manifest_file_map(backup_dir: str, domain: str) -> dict:
    """relativePath -> absolute path of the hashed backup file."""
    manifest = os.path.join(backup_dir, "Manifest.db")
    if not os.path.exists(manifest):
        sys.exit(f"Manifest.db not found in {backup_dir}")
    con = sqlite3.connect(manifest)
    try:
        rows = con.execute(
            "SELECT relativePath, fileID FROM Files WHERE domain = ? AND flags = 1",
            (domain,),
        ).fetchall()
    finally:
        con.close()
    if not rows:
        sys.exit(f"No files found for domain {domain} — wrong domain or encrypted backup?")
    return {rel: os.path.join(backup_dir, fid[:2], fid) for rel, fid in rows}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--chatstorage", required=True, help="Path to ChatStorage.sqlite")
    ap.add_argument("--backup-dir", required=True, help="MobileSync device backup directory")
    ap.add_argument("--domain", default=DEFAULT_DOMAIN,
                    help=f"Manifest domain (default: {DEFAULT_DOMAIN})")
    ap.add_argument("--out", required=True, help="Output bundle .zip path")
    ap.add_argument("--include-videos", action="store_true",
                    help="Also bundle .mp4/.mov originals (much larger)")
    args = ap.parse_args()

    refs = referenced_media_paths(args.chatstorage)
    fmap = manifest_file_map(os.path.expanduser(args.backup_dir), args.domain)

    stats = {"bundled": 0, "videos_excluded": 0, "missing": 0, "bytes": 0}
    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_STORED) as bundle:
        bundle.write(args.chatstorage, "ChatStorage.sqlite")
        for rel in refs:
            if rel.lower().endswith(VIDEO_EXTENSIONS) and not args.include_videos:
                stats["videos_excluded"] += 1
                continue
            src = fmap.get(f"Message/{rel}")
            if not src or not os.path.exists(src):
                stats["missing"] += 1
                continue
            bundle.write(src, rel)
            stats["bundled"] += 1
            stats["bytes"] += os.path.getsize(src)
            if stats["bundled"] % 500 == 0:
                print(f"  ... {stats['bundled']} files ({stats['bytes'] / 1e6:.0f} MB)")

    print(
        f"Done: {args.out}\n"
        f"  media bundled:   {stats['bundled']} ({stats['bytes'] / 1e6:.0f} MB)\n"
        f"  videos excluded: {stats['videos_excluded']}\n"
        f"  missing in backup: {stats['missing']}"
    )


if __name__ == "__main__":
    main()
