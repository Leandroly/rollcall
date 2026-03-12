import csv
from pathlib import Path

from flask import Flask, jsonify, render_template

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api.students")
@app.route("/api/students")
def api_students():
    students_path = Path(__file__).parent / "students.csv"
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


if __name__ == "__main__":
    app.run(debug=True)
