from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("benchmark_library", PROJECT_DIR / "tools" / "benchmark_library.py")
benchmark = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(benchmark)


class BenchmarkToolTests(unittest.TestCase):
    def test_percentile_uses_nearest_rank(self) -> None:
        self.assertEqual(benchmark.percentile([], 0.95), 0.0)
        self.assertEqual(benchmark.percentile([5.0], 0.5), 5.0)
        self.assertEqual(benchmark.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5), 3.0)
        self.assertEqual(benchmark.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.95), 5.0)

    def test_seeded_library_exposes_the_synthetic_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "library"
            benchmark.seed_library(home, 20)
            payload = benchmark.payload_size(home)
            self.assertEqual(payload["item_count"], 21)  # seed item + 20 synthetic mirrors
            self.assertGreater(payload["approx_json_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
