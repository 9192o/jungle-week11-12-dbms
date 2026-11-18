import tempfile
import hashlib
import json
import unittest
import zipfile
from pathlib import Path

from full_replay import describe, read_requests
from server import encode, load_traffic, prepare_ranking_files, read_top_rankings
from mini_dbms import Catalog, Executor, Select


class DemoTests(unittest.TestCase):
    def test_ranking_inputs_group_queries_by_selected_utc_hours(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "full-pageview-traffic.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("full-pageview-traffic/hourly-pageviews.csv",
                                 "date,hour_utc,id,views\n2026-10-25,9,3,10\n"
                                 "2026-10-25,9,1,10\n2026-10-25,8,3,11\n"
                                 "2026-11-01,20,3,12\n")
            files = prepare_ranking_files(path, Path(directory))
            self.assertEqual(set(files), {"2026-10-25T09", "2026-11-01T20"})
            self.assertEqual(files["2026-10-25T09"].read_bytes(), b"10,-3\n10,-1\n")
            self.assertEqual(read_top_rankings(files["2026-10-25T09"]), [(10, -1), (10, -3)])
            self.assertEqual(read_top_rankings(files["2026-11-01T20"]), [(12, -3)])

    def test_full_replay_preserves_order_and_repeated_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "full-replay.csv"
            path.write_text(
                "seq,timestamp_utc,id\n1,2026-10-25T00:00:00.049Z,5\n"
                "2,2026-11-01T00:00:00.000Z,5\n", encoding="utf-8"
            )
            self.assertEqual([row[2] for row in read_requests(path)], [5, 5])
            self.assertEqual(describe(path)["dates"], {"2026-10-25": 1, "2026-11-01": 1})
            with path.open("a", encoding="utf-8") as file:
                file.write("3,2026-10-25T00:00:00.049Z,6\n")
            with self.assertRaisesRegex(ValueError, "Invalid replay row 3"):
                list(read_requests(path))

    def test_traffic_validates_hash_and_preserves_duplicate_requests(self):
        hourly = b"date,hour_utc,id,views\n2026-10-25,0,5,3\n"
        pilot = {
            "articles_csv_sha256": "articles", "seed": 42, "requests_per_date": 2,
            "popularity_source_sha256": hashlib.sha256(hourly).hexdigest(),
            "source_stats": {"2026-10-25": {"hour_id_rows": 1}},
            "requests": [{"seq": i, "date": "2026-10-25", "hour_utc": 0, "id": 5} for i in (1, 2)],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "full-pageview-traffic.zip"
            self.assertIsNone(load_traffic(path, "articles"))
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("full-pageview-traffic/hourly-pageviews.csv", hourly)
                archive.writestr("full-pageview-traffic/pilot-replay.json", json.dumps(pilot))
            self.assertEqual([row["id"] for row in load_traffic(path, "articles")["requests"]], [5, 5])
            with self.assertRaisesRegex(ValueError, "different articles"):
                load_traffic(path, "another file")

    def test_linear_id_stops_at_hit_but_title_and_missing_scan_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "articles.csv"
            path.write_bytes(b"id:int,title:string\n4,same\n2,other\n7,same\n")
            executor = Executor(Catalog(path.parent))
            for field, value, read_count, expected in (
                ("id", 4, 1, [[4, "same"]]),
                ("id", 2, 2, [[2, "other"]]),
                ("id", 8, 3, []),
                ("title", "same", 3, [[4, "same"], [7, "same"]]),
            ):
                visits = []
                result = executor.execute(Select("articles", where=(field, value)),
                                          use_index=False, on_csv_row=lambda: visits.append(1))
                self.assertEqual(result.rows, expected)
                self.assertEqual(len(visits), read_count)

    def test_resp_uses_utf8_byte_length(self):
        self.assertEqual(encode("SET", "7", "한"),
                         b"*3\r\n$3\r\nSET\r\n$1\r\n7\r\n$3\r\n" + "한".encode() + b"\r\n")
