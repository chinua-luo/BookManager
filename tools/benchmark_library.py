"""Reproducible library benchmark for BookManager performance work.

Creates a temporary library with N synthetic mirrors that all reference one
real document, then measures store-level and bridge-level hot paths.

Usage:
    python3 tools/benchmark_library.py --items 5000
    python3 tools/benchmark_library.py --items 5000 --repeats 3 --no-api
"""

from __future__ import annotations

import argparse
import http.client
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

from bookmanager.bridge import BridgeRequestHandler  # noqa: E402
from bookmanager.file_copy import clone_or_copy  # noqa: E402
from bookmanager.naming import infer_structured_file_name  # noqa: E402
from bookmanager.startup import check_storage  # noqa: E402
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


def p1_benchmarks(root: Path) -> dict[str, object]:
    """Scenarios targeted by the P1 work: imports, copies, cache and startup."""
    import threading

    report: dict[str, object] = {}

    import_home = root / "p1-import"
    big = root / "p1-big.bin"
    big.write_bytes(b"\0" * (128 * 1024 * 1024))
    timings: dict[str, float] = {}
    started = threading.Event()

    def import_worker() -> None:
        store = LibraryStore(import_home)
        started.set()
        try:
            began = time.perf_counter()
            store.add_local_file(big, 1)
            timings["import_s"] = time.perf_counter() - began
        finally:
            store.close()

    thread = threading.Thread(target=import_worker)
    thread.start()
    started.wait()
    writer = None
    latencies = []
    while thread.is_alive():
        writer = LibraryStore(import_home)
        began = time.perf_counter()
        writer.create_folder(1, f"during-{len(latencies)}")
        latencies.append((time.perf_counter() - began) * 1000)
        writer.close()
        time.sleep(0.005)
    thread.join()
    report["import_128mb"] = {
        "seconds": round(timings.get("import_s", 0.0), 3),
        "max_concurrent_write_ms": round(max(latencies) if latencies else 0.0, 1),
    }

    copy_source = root / "p1-copy.bin"
    copy_source.write_bytes(b"\0" * (256 * 1024 * 1024))
    plain = root / "p1-plain.bin"
    began = time.perf_counter()
    shutil.copy2(copy_source, plain)
    report["copy_256mb_plain_ms"] = round((time.perf_counter() - began) * 1000, 1)
    clone = root / "p1-clone.bin"
    began = time.perf_counter()
    clone_or_copy(copy_source, clone)
    report["copy_256mb_clone_ms"] = round((time.perf_counter() - began) * 1000, 1)
    plain.unlink(missing_ok=True)
    clone.unlink(missing_ok=True)

    folder = root / "p1-folder"
    folder.mkdir()
    for index in range(400):
        (folder / f"book-{index}.txt").write_text(f"content {index}", encoding="utf-8")
    folder_home = root / "p1-folder-home"
    folder_store = LibraryStore(folder_home)
    handler = object.__new__(BridgeRequestHandler)
    began = time.perf_counter()
    result = BridgeRequestHandler._import_folder_tree(handler, folder_store, folder, 1, ())
    report["folder_import_400_s"] = round(time.perf_counter() - began, 2)
    folder_store.close()

    cache_home = root / "p1-cache"
    cache_store = LibraryStore(cache_home)
    for index in range(200):
        directory = cache_store.prepare_cache_directory("open_cache", f"entry-{index}")
        (directory / "file.txt").write_bytes(b"x" * (1024 * 1024))
        cache_store.register_open_cache_copy(directory, 1, "deadbeef", "file.txt")
    began = time.perf_counter()
    cache_store.cache_usage()
    report["cache_usage_200_ms"] = round((time.perf_counter() - began) * 1000, 1)
    began = time.perf_counter()
    cache_store.sync_open_cache_changes()
    report["sync_open_cache_200_ms"] = round((time.perf_counter() - began) * 1000, 1)
    cache_store.close()

    startup_home = root / "p1-startup"
    startup_store = LibraryStore(startup_home)
    for index in range(200):
        directory = startup_store.prepare_cache_directory("open_cache", f"entry-{index}")
        (directory / "file.txt").write_bytes(b"x" * (1024 * 1024))
        old = time.time() - 30 * 24 * 3600
        os.utime(directory, (old, old))
        os.utime(directory / "file.txt", (old, old))
    startup_store.close()
    static_dir = PROJECT_DIR / "electron" / "dist"
    if (static_dir / "index.html").is_file():
        from bookmanager.bridge import BridgeServer

        began = time.perf_counter()
        server = BridgeServer(("127.0.0.1", 0), startup_home, "b" * 64, static_dir)
        report["startup_ready_ms"] = round((time.perf_counter() - began) * 1000, 1)
        began = time.perf_counter()
        server.wait_for_startup_cleanup(60)
        report["startup_cleanup_ms"] = round((time.perf_counter() - began) * 1000, 1)
        server.server_close()

    check_home = root / "p1-check"
    check_store = LibraryStore(check_home)
    check_store.conn.executemany(
        "INSERT INTO folders(parent_id,name,created_at) VALUES(1,?,?)",
        [(f"F{index}", "2026-01-01T00:00:00Z") for index in range(20000)],
    )
    check_store.conn.commit()
    check_store.mark_clean_shutdown()
    check_store.close()
    began = time.perf_counter()
    check_storage(check_home)
    report["check_storage_20000_clean_ms"] = round((time.perf_counter() - began) * 1000, 1)
    began = time.perf_counter()
    check_storage(check_home)
    report["check_storage_20000_full_ms"] = round((time.perf_counter() - began) * 1000, 1)
    return report


def p2_benchmarks(root: Path) -> dict[str, object]:
    """Scenarios targeted by the P2 work: WAL reads, bounded text previews."""
    import threading

    report: dict[str, object] = {}
    home = root / "p2-wal"
    seed_library(home, 5000)
    probe = LibraryStore(home)
    try:
        report["journal_mode"] = probe.conn.execute("PRAGMA journal_mode").fetchone()[0]
        report["busy_timeout_ms"] = int(probe.conn.execute("PRAGMA busy_timeout").fetchone()[0])
    finally:
        probe.close()

    latencies: list[float] = []
    done = threading.Event()

    def writer() -> None:
        store = LibraryStore(home)
        try:
            for index in range(500):
                with store.transaction():
                    store.conn.execute("UPDATE cache_settings SET open_days = ? WHERE id = 1", (7 + index % 2,))
        finally:
            store.close()
            done.set()

    def reader() -> None:
        store = LibraryStore(home)
        try:
            while not done.is_set() or len(latencies) < 20:
                began = time.perf_counter()
                store.list_items(1)
                latencies.append((time.perf_counter() - began) * 1000)
        finally:
            store.close()

    reader_thread = threading.Thread(target=reader)
    writer_thread = threading.Thread(target=writer)
    reader_thread.start()
    writer_thread.start()
    writer_thread.join()
    done.set()
    reader_thread.join()
    report["reader_during_writes_ms"] = {
        "samples": len(latencies),
        "p50_ms": round(statistics.median(latencies), 1),
        "p95_ms": round(percentile(latencies, 0.95), 1),
        "max_ms": round(max(latencies), 1),
    }

    big = root / "p2-big.txt"
    big.write_text("x" * (10 * 1024 * 1024), encoding="utf-8")
    store = LibraryStore(home)
    try:
        item = store.get_item(store.add_local_file(big, 1))
        document_id = item.document_id
    finally:
        store.close()

    from bookmanager.bridge import BridgeServer

    token = "b" * 64
    server = BridgeServer(("127.0.0.1", 0), home, token, PROJECT_DIR / "electron" / "dist")
    server.wait_for_startup_cleanup(60)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        def fetch_text(range_header: str | None = None) -> dict[str, float]:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=120)
            headers = {"Authorization": "Bearer " + token}
            if range_header:
                headers["Range"] = range_header
            began = time.perf_counter()
            connection.request("GET", f"/api/documents/{document_id}/content", headers=headers)
            response = connection.getresponse()
            body = response.read()
            elapsed = (time.perf_counter() - began) * 1000
            connection.close()
            return {"status": response.status, "bytes": len(body), "ms": round(elapsed, 1)}

        report["text_preview_full"] = fetch_text()
        report["text_preview_range_2mb"] = fetch_text("bytes=0-2097151")
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
    return report


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
    parser.add_argument("--p1", action="store_true", help="also run P1 import/copy/cache/startup scenarios")
    parser.add_argument("--p2", action="store_true", help="also run P2 WAL/text-preview scenarios")
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
        if args.p1:
            report["p1"] = p1_benchmarks(Path(tempfile.mkdtemp(prefix="bm-benchmark-p1-")))
        if args.p2:
            report["p2"] = p2_benchmarks(Path(tempfile.mkdtemp(prefix="bm-benchmark-p2-")))
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
            for name, value in report.get("p1", {}).items():
                print(f"  {name:<28} {value}")
            for name, value in report.get("p2", {}).items():
                print(f"  {name:<28} {value}")
        return 0
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
