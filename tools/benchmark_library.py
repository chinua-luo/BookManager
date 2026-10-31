"""Reproducible library benchmark for BookManager performance work.

Creates a temporary library with N synthetic mirrors that all reference one
real document, then measures store-level and bridge-level hot paths.

Usage:
    python3 tools/benchmark_library.py --items 5000
    python3 tools/benchmark_library.py --items 5000 --repeats 3 --no-api
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from bookmanager.naming import infer_structured_file_name  # noqa: E402
from bookmanager.store import LibraryStore  # noqa: E402


def percentile(values: list[float], ratio: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round(ratio * (len(ordered) - 1))))
    return ordered[index]


def timed(repeats: int, action) -> dict[str, float]:
    samples: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        action()
        samples.append((time.perf_counter() - started) * 1000)
    return {
        "p50_ms": round(statistics.median(samples), 1),
        "p95_ms": round(percentile(samples, 0.95), 1),
        "min_ms": round(min(samples), 1),
        "max_ms": round(max(samples), 1),
    }


def seed_library(home: Path, item_count: int) -> int:
    store = LibraryStore(home)
    try:
        source = home / "seed.txt"
        source.write_text("seed content", encoding="utf-8")
        seed_id = store.add_local_file(source, 1, infer_structured_file_name("Seed.txt"))
        document_id = store.get_item(seed_id).document_id
        nested = store.create_folder(1, "Series")
        nested = store.create_folder(nested, "Volume")
        tag_cursor = store.conn.execute("INSERT INTO tags(name, normalized_name) VALUES('article', 'article')")
        tag_id = int(tag_cursor.lastrowid)
        rows = []
        for index in range(item_count):
            name_data = json.dumps(
                {
                    "series_abbr": "",
                    "number": "",
                    "main_title": f"Synthetic Book {index:05d}",
                    "subtitle": f"Volume {index % 10}",
                    "edition": "1",
                    "edition_language": "英文",
                    "authors": "Some Author",
                    "extension": ".pdf",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            title = f"Synthetic Book {index:05d}"
            subtitle = f"Volume {index % 10}"
            rows.append(
                (
                    1,
                    document_id,
                    f"{title} _ Some Author.pdf",
                    name_data,
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                    title.lower(),
                    subtitle.lower(),
                    "some author",
                )
            )
        store.conn.executemany(
            "INSERT INTO items(folder_id, document_id, display_name, name_data, created_at, updated_at, "
            "main_title_norm, subtitle_norm, authors_norm) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        store.conn.executemany(
            "INSERT INTO item_tags(item_id, tag_id) VALUES(?, ?)",
            [(int(row[0]), tag_id) for row in store.conn.execute("SELECT id FROM items WHERE id > ?", (seed_id,))],
        )
        store.conn.commit()
        return document_id
    finally:
        store.close()


def store_benchmarks(home: Path, repeats: int) -> dict[str, dict[str, float]]:
    results: dict[str, dict[str, float]] = {}
    results["store_open"] = timed(repeats, lambda: LibraryStore(home).close())

    store = LibraryStore(home)
    try:
        results["list_items"] = timed(repeats, lambda: store.list_items(1))
        results["folder_stats"] = timed(repeats, lambda: store.folder_stats(1))
        results["search_unique"] = timed(repeats, lambda: store.search_unique_documents("Synthetic", limit=500))
        results["search_mirrors"] = timed(repeats, lambda: store.search_mirror_items("Synthetic", limit=500))
        results["search_tags"] = timed(repeats, lambda: store.search_tagged_items("article", limit=500))
        results["tags_by_frequency"] = timed(repeats, store.tags_by_frequency)
    finally:
        store.close()
    return results


def _request(base: str, token: str, path: str, payload: dict | None = None) -> tuple[int, dict, int]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base + path, data=data, method="POST" if data else "GET")
    request.add_header("Authorization", "Bearer " + token)
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read()
            return response.status, json.loads(body), len(body)
    except urllib.error.HTTPError as error:
        body = error.read()
        return error.code, json.loads(body), len(body)


def api_benchmarks(home: Path, repeats: int) -> dict[str, dict[str, float]]:
    token = "b" * 64
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "bookmanager.startup",
            "--port",
            "0",
            "--static-dir",
            str(PROJECT_DIR / "electron" / "dist"),
            "--data-home",
            str(home),
        ],
        cwd=str(PROJECT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "BOOKMANAGER_BRIDGE_TOKEN": token, "BOOKMANAGER_DIAGNOSTIC_HOME": str(home / "diag")},
    )
    port = None
    deadline = time.time() + 60
    try:
        while time.time() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "ready":
                port = int(event["port"])
                break
            if event.get("event") == "startup-error":
                raise RuntimeError(f"bridge failed to start: {event}")
        if port is None:
            raise RuntimeError("bridge did not report a port")
        base = f"http://127.0.0.1:{port}"
        results: dict[str, dict[str, float]] = {}
        results["api_tree"] = timed(repeats, lambda: _request(base, token, "/api/tree"))
        results["api_settings"] = timed(repeats, lambda: _request(base, token, "/api/settings"))
        results["api_tags"] = timed(repeats, lambda: _request(base, token, "/api/tags"))
        results["api_folder_contents"] = timed(
            repeats, lambda: _request(base, token, "/api/folders/1/contents")
        )
        results["api_folder_page"] = timed(
            repeats, lambda: _request(base, token, "/api/folders/1/contents?offset=0&limit=200")
        )
        results["api_search_files"] = timed(
            repeats,
            lambda: _request(base, token, "/api/search", {"kind": "files", "mainTitle": "Synthetic"}),
        )
        results["api_search_mirrors"] = timed(
            repeats,
            lambda: _request(base, token, "/api/search", {"kind": "mirrors", "mainTitle": "Synthetic"}),
        )
        results["api_search_tags"] = timed(
            repeats,
            lambda: _request(base, token, "/api/search", {"kind": "tags", "query": "article"}),
        )
        sizes = {}
        for name, path, payload in (
            ("api_folder_page_bytes", "/api/folders/1/contents?offset=0&limit=200", None),
            ("api_search_files_bytes", "/api/search", {"kind": "files", "mainTitle": "Synthetic"}),
        ):
            status, _body, size = _request(base, token, path, payload)
            if status == 200:
                sizes[name] = size
        results["_bytes"] = sizes
        return results
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()


def payload_size(home: Path) -> dict[str, int]:
    # A cheap approximation of the JSON body users have to download per folder click.
    store = LibraryStore(home)
    try:
        items = store.list_items(1)
        payload = [
            {"id": item.id, "displayName": item.display_name, "listName": item.display_name,
             "extension": item.extension if hasattr(item, "extension") else item.name_parts.extension,
             "size": item.size, "mime": item.mime, "note": item.note, "tags": list(item.tags)}
            for item in items
        ]
        return {"item_count": len(items), "approx_json_bytes": len(json.dumps(payload, ensure_ascii=False))}
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=int, default=5000, help="synthetic mirror count")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--no-api", action="store_true", help="skip bridge HTTP benchmarks")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    home = Path(tempfile.mkdtemp(prefix="bm-benchmark-"))
    try:
        seed_library(home, args.items)
        report: dict = {
            "items": args.items,
            "repeats": args.repeats,
            "payload": payload_size(home),
            "store": store_benchmarks(home, args.repeats),
        }
        if not args.no_api:
            report["api"] = api_benchmarks(home, args.repeats)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"items: {report['items']}  repeats: {report['repeats']}  "
                  f"payload: {report['payload']['approx_json_bytes'] / 1024:.0f} KiB")
            for section in ("store", "api"):
                for name, values in report.get(section, {}).items():
                    if name == "_bytes":
                        continue
                    print(f"  {name:<22} p50 {values['p50_ms']:>8.1f} ms   p95 {values['p95_ms']:>8.1f} ms")
            for name, size in report.get("api", {}).get("_bytes", {}).items():
                print(f"  {name:<22} {size / 1024:>8.1f} KiB")
        return 0
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
