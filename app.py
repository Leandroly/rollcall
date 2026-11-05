import csv
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, current_app, jsonify, render_template

BASE_DIR = Path(__file__).resolve().parent
# Keep the completed groups and selection stages on the server.
ALREADY_DRAWN_GROUP_IDS = frozenset({1, 2, 4, 10, 14, 17})
PRIORITY_GROUP_IDS = frozenset({3, 6, 8, 9, 16})
FINAL_GROUP_IDS = frozenset({11, 12})


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        GROUPS_PATH=BASE_DIR / "groups.csv",
        STUDENTS_PATH=BASE_DIR / "students.csv",
        DRAW_DATABASE=Path(app.instance_path) / "draws.sqlite3",
        DRAW_TIMEZONE="America/New_York",
    )
    if test_config is not None:
        app.config.update(test_config)

    app.add_url_rule("/", view_func=index)
    app.add_url_rule("/api.students", view_func=api_students)
    app.add_url_rule("/api/students", view_func=api_students)
    app.add_url_rule("/api/groups", view_func=api_groups)
    app.add_url_rule("/api/groups/draw", view_func=draw_group, methods=["POST"])
    app.add_url_rule("/api/groups/reset", view_func=reset_draws, methods=["POST"])
    return app


def index():
    return render_template("index.html")


def api_students():
    students_path = Path(current_app.config["STUDENTS_PATH"])
    names = []

    with students_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.reader(csv_file)
        next(reader, None)  # Skip header row

        for row in reader:
            if not row:
                continue

            # First column is the student's full name.
            raw_name = row[0].strip()
            cleaned_name = raw_name.lstrip("- ").strip()

            if cleaned_name:
                names.append(cleaned_name)

    return jsonify(names)


def load_groups():
    groups = {}
    columns = {}
    with Path(current_app.config["GROUPS_PATH"]).open(
        "r", encoding="utf-8-sig", newline=""
    ) as csv_file:
        for row in csv.reader(csv_file):
            cells = [(column, value.strip()) for column, value in enumerate(row) if value.strip()]
            if not cells:
                continue

            headers = [re.fullmatch(r"(?:Team\s*)?(\d+)", value, re.IGNORECASE) for _, value in cells]
            if all(headers):
                columns = {}
                for (column, _), header in zip(cells, headers):
                    team_id = int(header.group(1))
                    columns[column] = team_id
                    groups[team_id] = {"id": team_id, "name": f"Team {team_id}", "members": []}
                continue

            for column, team_id in columns.items():
                if column < len(row) and row[column].strip():
                    groups[team_id]["members"].append(row[column].strip())

    return [group for group in groups.values() if group["members"]]


def load_drawable_groups():
    return [group for group in load_groups() if group["id"] not in ALREADY_DRAWN_GROUP_IDS]


@contextmanager
def draw_history():
    database_path = Path(current_app.config["DRAW_DATABASE"])
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(database_path, timeout=10)
    try:
        database.execute(
            "CREATE TABLE IF NOT EXISTS drawn_groups "
            "(team_id INTEGER PRIMARY KEY, drawn_at TEXT NOT NULL)"
        )
        yield database
    finally:
        database.close()


def drawn_ids(database):
    return {row[0] for row in database.execute("SELECT team_id FROM drawn_groups")}


def api_groups():
    with draw_history() as database:
        selected = drawn_ids(database)
    # The animation shows every remaining group, without exposing selection rules.
    response = jsonify([group for group in load_drawable_groups() if group["id"] not in selected])
    response.headers["Cache-Control"] = "no-store"
    return response


def current_draw_time():
    return datetime.now(ZoneInfo(current_app.config["DRAW_TIMEZONE"]))


def reset_draws():
    groups = load_drawable_groups()
    with draw_history() as database:
        database.execute("BEGIN IMMEDIATE")
        database.execute("DELETE FROM drawn_groups")
        database.commit()

    response = jsonify(groups)
    response.headers["Cache-Control"] = "no-store"
    return response


def draw_group():
    groups = load_drawable_groups()
    with draw_history() as database:
        # Serialize selection and recording across tabs and server processes.
        database.execute("BEGIN IMMEDIATE")
        selected = drawn_ids(database)
        now = current_draw_time()
        remaining = [group for group in groups if group["id"] not in selected]
        # Finish each stage before the next; draw randomly within the active stage.
        candidates = (
            [group for group in remaining if group["id"] in PRIORITY_GROUP_IDS]
            or [group for group in remaining if group["id"] not in FINAL_GROUP_IDS]
            or remaining
        )
        if not candidates:
            database.rollback()
            return jsonify(error="No groups available for this draw"), 409

        winner = secrets.choice(candidates)
        database.execute(
            "INSERT INTO drawn_groups (team_id, drawn_at) VALUES (?, ?)",
            (winner["id"], now.isoformat()),
        )
        database.commit()

    return jsonify(winner)


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
