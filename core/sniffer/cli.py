"""
CLI tool for importing sniffer captures into Ride-the-API.

Usage:
    python -m core.sniffer import --file capture.pcapng --device dev-1
    python -m core.sniffer import --file capture.har --device dev-1
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

from core.atomic_io import sanitize_filename_component
from core.database.manager import DatabaseManager
from core.sniffer.service import SnifferService

HTTP_STATUS_BAD_REQUEST = 400


async def _run_import(file_path: Path, device_id: str, server_url: str | None = None) -> None:
    if not file_path.exists():
        sys.stderr.write(f"Error: File not found: {file_path}\n")
        sys.exit(1)

    content = file_path.read_bytes()
    filename = file_path.name

    if server_url:
        url = f"{server_url.rstrip('/')}/api/devices/{device_id}/sniffer/import"
        print(f"Uploading {filename} ({len(content)} bytes) to {url}...")  # noqa: T201
        async with httpx.AsyncClient() as client:
            files = {"file": (filename, content, "application/octet-stream")}
            resp = await client.post(url, files=files, timeout=60.0)
            if resp.status_code >= HTTP_STATUS_BAD_REQUEST:
                sys.stderr.write(f"API Error ({resp.status_code}): {resp.text}\n")
                sys.exit(1)
            data = resp.json()
            fmt = data.get("format")
            imported = data.get("imported", 0)
            print(f"Success! Imported {imported} pairs (format: {fmt}).")  # noqa: T201
            return

    # Direct import using local database
    db_dir = Path("data/databases")
    db_dir.mkdir(parents=True, exist_ok=True)
    db_manager = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{db_dir / 'core.db'}",
        device_db_dir=db_dir,
    )
    await db_manager.initialize()

    service = SnifferService(db_manager)
    print(f"Parsing and importing {filename} ({len(content)} bytes) into '{device_id}'...")  # noqa: T201
    try:
        res = await service.import_to_device(
            device_id=device_id,
            content=content,
            filename=filename,
        )
        fmt = res.get("format")
        imported = res.get("imported", 0)
        print(f"Success! Imported {imported} pairs (format: {fmt}).")  # noqa: T201
        print("Summary of imported pairs:")  # noqa: T201
        for p in res.get("pairs_summary", [])[:10]:
            print(f"  {p.get('method')} {p.get('path')} -> {p.get('status')}")  # noqa: T201
    finally:
        await db_manager.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Ride-the-API Sniffer Capture Importer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser(
        "import", help="Import a PCAP, PCAPNG, or HAR capture file"
    )
    import_parser.add_argument(
        "--file", "-f", required=True, type=Path, help="Path to capture file (.har, .pcap, .pcapng)"
    )
    import_parser.add_argument("--device", "-d", required=True, type=str, help="Target device ID")
    import_parser.add_argument(
        "--url", "-u", type=str, default=None, help="Server URL (e.g. http://localhost:8911)"
    )

    args = parser.parse_args()
    if args.command == "import":
        device_id = sanitize_filename_component(args.device)
        asyncio.run(_run_import(args.file, device_id, args.url))


if __name__ == "__main__":
    main()
