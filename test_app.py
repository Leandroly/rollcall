import csv
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import BASE_DIR, create_app, current_draw_time, load_groups


class GroupDrawTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="rollcall-test-", dir=BASE_DIR)
        self.addCleanup(self.directory.cleanup)
        self.database_path = Path(self.directory.name) / "draws.sqlite3"
        self.config = {"TESTING": True, "DRAW_DATABASE": self.database_path}
        self.app = create_app(self.config)
        self.client = self.app.test_client()

    def draw_all_available(self, now):
        winners = []
        with patch("app.current_draw_time", return_value=now):
            while True:
                response = self.client.post("/api/groups/draw")
                if response.status_code == 409:
                    break
                self.assertEqual(response.status_code, 200)
                winners.append(response.get_json()["id"])
        return winners

    def test_roster_and_group_counts(self):
        with self.app.app_context():
            groups = load_groups()
        self.assertEqual([group["id"] for group in groups], list(range(1, 19)))
        members = [name for group in groups for name in group["members"]]
        students = self.client.get("/api/students").get_json()
        self.assertEqual(len(members), 64)
        self.assertEqual(len(students), len(members))
        self.assertEqual(len(set(name.casefold() for name in members)), len(members))
        self.assertEqual(len(set(name.casefold() for name in students)), len(students))
        self.assertEqual(next(group for group in groups if group["id"] == 15)["members"],
                         ["Kriti Tyagi", "Gabriella Fama"])

    def test_group_parser_handles_block_headers_and_empty_cells(self):
        fixture = Path(self.directory.name) / "groups.csv"
        with fixture.open("w", encoding="utf-8-sig", newline="") as csv_file:
            csv.writer(csv_file).writerows([
                ["Team1", "", "2"],
                [" First Student ", "", ""],
                ["", "", "Second Student"],
                [],
                ["7"],
                ["Third Student"],
            ])
        self.app.config["GROUPS_PATH"] = fixture
        with self.app.app_context():
            self.assertEqual(load_groups(), [
                {"id": 1, "name": "Team 1", "members": ["First Student"]},
                {"id": 2, "name": "Team 2", "members": ["Second Student"]},
                {"id": 7, "name": "Team 7", "members": ["Third Student"]},
            ])

    def test_restrictions_on_every_weekday(self):
        monday = datetime(2026, 11, 2, 12, tzinfo=ZoneInfo("America/New_York"))
        for weekday in range(7):
            with self.subTest(weekday=weekday):
                self.app.config["DRAW_DATABASE"] = Path(self.directory.name) / f"day-{weekday}.sqlite3"
                winners = self.draw_all_available(monday + timedelta(days=weekday))
                expected = set(range(1, 19)) - {5, 6, 7, 15}
                if weekday == 1:
                    expected.add(6)
                if weekday == 3:
                    expected.update({5, 7, 15})
                self.assertEqual(set(winners), expected)
                self.assertEqual(len(winners), len(set(winners)))

    def test_history_survives_new_app_and_different_days(self):
        tuesday = datetime(2026, 11, 3, 12, tzinfo=ZoneInfo("America/New_York"))
        tuesday_winners = self.draw_all_available(tuesday)
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        remaining = self.client.get("/api/groups").get_json()
        self.assertEqual({group["id"] for group in remaining}, {5, 7, 15})
        thursday_winners = self.draw_all_available(tuesday + timedelta(days=2))
        all_winners = tuesday_winners + thursday_winners
        self.assertEqual(len(all_winners), 18)
        self.assertEqual(len(set(all_winners)), 18)
        self.assertEqual(self.client.get("/api/groups").get_json(), [])

    def test_parallel_requests_do_not_repeat_groups(self):
        now = datetime(2026, 11, 5, 12, tzinfo=ZoneInfo("America/New_York"))
        self.client.get("/api/groups")

        def draw_once(_):
            with self.app.test_client() as client:
                response = client.post("/api/groups/draw")
                return response.status_code, response.get_json()

        with patch("app.current_draw_time", return_value=now):
            with ThreadPoolExecutor(max_workers=8) as executor:
                responses = list(executor.map(draw_once, range(24)))
        self.assertTrue(all(status in (200, 409) for status, _ in responses))
        winners = [data["id"] for status, data in responses if status == 200]
        self.assertEqual(len(winners), 17)
        self.assertEqual(len(set(winners)), 17)
        self.assertNotIn(6, winners)
        with closing(sqlite3.connect(self.database_path)) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM drawn_groups").fetchone()[0], 17)

    def test_reset_restores_all_groups_and_starts_a_new_round(self):
        tuesday = datetime(2026, 11, 3, 12, tzinfo=ZoneInfo("America/New_York"))
        self.draw_all_available(tuesday)
        self.draw_all_available(tuesday + timedelta(days=2))
        self.assertEqual(self.client.get("/api/groups").get_json(), [])

        response = self.client.post("/api/groups/reset")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual({group["id"] for group in response.get_json()}, set(range(1, 19)))

        self.app = create_app(self.config)
        self.client = self.app.test_client()
        self.assertEqual(len(self.client.get("/api/groups").get_json()), 18)
        winners = self.draw_all_available(tuesday)
        self.assertEqual(set(winners), set(range(1, 19)) - {5, 7, 15})
        self.assertEqual(len(winners), len(set(winners)))

    def test_reset_requires_post_and_handles_empty_history(self):
        tuesday = datetime(2026, 11, 3, 12, tzinfo=ZoneInfo("America/New_York"))
        with patch("app.current_draw_time", return_value=tuesday):
            self.assertEqual(self.client.post("/api/groups/draw").status_code, 200)
        self.assertEqual(self.client.get("/api/groups/reset").status_code, 405)
        self.assertEqual(len(self.client.get("/api/groups").get_json()), 17)
        for _ in range(2):
            response = self.client.post("/api/groups/reset")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.get_json()), 18)

    def test_rules_stay_out_of_public_group_data(self):
        response = self.client.get("/api/groups")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(len(response.get_json()), 18)
        for group in response.get_json():
            self.assertEqual(set(group), {"id", "name", "members"})
        html = self.client.get("/").get_data(as_text=True).lower()
        for rule in ("tuesday", "thursday", "weekday", "draw_weekdays"):
            self.assertNotIn(rule, html)

    def test_draw_date_uses_new_york_timezone(self):
        # Tuesday UTC is still Monday in New York at this instant.
        instant = datetime(2026, 11, 3, 2, tzinfo=timezone.utc)
        with self.app.app_context(), patch("app.datetime") as clock:
            clock.now.side_effect = lambda zone: instant.astimezone(zone)
            local_time = current_draw_time()
            self.assertEqual(local_time.weekday(), 0)
            self.assertEqual(str(clock.now.call_args.args[0]), "America/New_York")
        winners = self.draw_all_available(local_time)
        self.assertNotIn(6, winners)

    def test_client_cannot_choose_winner_or_weekday(self):
        monday = datetime(2026, 11, 2, 12, tzinfo=ZoneInfo("America/New_York"))
        with patch("app.current_draw_time", return_value=monday):
            response = self.client.post("/api/groups/draw?weekday=1&team_id=6", json={"team_id": 6})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(response.get_json()["id"], {5, 6, 7, 15})
        self.assertEqual(self.client.get("/api/groups/draw").status_code, 405)

    def test_legacy_student_endpoint(self):
        self.assertEqual(self.client.get("/api.students").get_json(),
                         self.client.get("/api/students").get_json())


if __name__ == "__main__":
    unittest.main()
