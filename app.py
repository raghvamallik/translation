# Gov Translation Tracker — Vercel-ready (Flask + Postgres)
# -------------------------------------------------------
# Runs on Vercel Functions (Python Runtime, WSGI). Uses a hosted Postgres DB.
# Local dev still works with Postgres if DATABASE_URL is set.
#
# What changed vs the SQLite version?
# - Switched storage from SQLite to Postgres (Vercel cannot persist SQLite files)
# - Uses psycopg3 with simple SQL (no ORM) and namedtuple rows for Jinja compatibility
# - Same UI and fields. Auto Doc ID: DOC-00001
#
# Files you also need in the repo (shown below in chat):
# - requirements.txt
# - api/index.py
# - vercel.json (rewrites everything to /api/index.py)
#
# Env vars required on Vercel:
# - DATABASE_URL (or POSTGRES_URL) — e.g. postgresql://user:pass@host/db

from __future__ import annotations
import os
import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import (
    Flask,
    g,
    redirect,
    render_template_string,
    request,
    send_file,
    url_for,
)

import psycopg
from psycopg.rows import namedtuple_row

APP_TITLE = "Gov Translation Tracker"

# --------------------- DB helpers ---------------------

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
if not DATABASE_URL:
    # Running on Vercel requires a hosted DB. For local dev, set DATABASE_URL.
    raise RuntimeError(
        "DATABASE_URL (or POSTGRES_URL) is not set. Provision a Postgres DB "
        "(e.g. Neon / Vercel Postgres) and set the connection string as an env var."
    )

app = Flask(__name__)


def get_db() -> psycopg.Connection:
    if "db" not in g:
        # A fresh connection per request is simplest for serverless usage.
        g.db = psycopg.connect(DATABASE_URL, row_factory=namedtuple_row)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS work_items (
    id SERIAL PRIMARY KEY,
    doc_id TEXT UNIQUE,
    go_number TEXT NOT NULL,
    translators TEXT,
    deputy_director TEXT,
    typist TEXT,
    arrival_date DATE NOT NULL,
    submission_date DATE,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_work_items_doc_id ON work_items(doc_id);
CREATE INDEX IF NOT EXISTS idx_work_items_go_number ON work_items(go_number);
"""


def init_db():
    db = get_db()
    with db.cursor() as cur:
        cur.execute(SCHEMA_SQL)
        db.commit()


@app.before_request
def ensure_db():
    # Ensure tables exist on cold starts
    init_db()


# --------------------- Utilities ---------------------

def normalize_date(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s.strip()


# --------------------- Routes ---------------------

@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "all")  # all | inprogress | submitted
    db = get_db()

    conditions = []
    params = []

    if q:
        # Case-insensitive search (ILIKE)
        conditions.append(
            "(doc_id ILIKE %s OR go_number ILIKE %s OR translators ILIKE %s OR "
            "deputy_director ILIKE %s OR typist ILIKE %s)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like, like])

    if status == "inprogress":
        conditions.append("submission_date IS NULL")
    elif status == "submitted":
        conditions.append("submission_date IS NOT NULL")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
        SELECT id, doc_id, go_number, translators, deputy_director, typist,
               TO_CHAR(arrival_date, 'YYYY-MM-DD') AS arrival_date,
               TO_CHAR(submission_date, 'YYYY-MM-DD') AS submission_date,
               CASE WHEN submission_date IS NULL THEN 'In Progress' ELSE 'Submitted' END AS status
          FROM work_items
          {where}
         ORDER BY id DESC
    """
    with db.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return render_template_string(TEMPLATE_INDEX, app_title=APP_TITLE, rows=rows, q=q, status=status)


@app.route("/add", methods=["GET", "POST"])
def add():
    if request.method == "POST":
        go_number = request.form.get("go_number", "").strip()
        translators = request.form.get("translators", "").strip()
        deputy_director = request.form.get("deputy_director", "").strip()
        typist = request.form.get("typist", "").strip()
        arrival_date = normalize_date(request.form.get("arrival_date"))
        submission_date = normalize_date(request.form.get("submission_date"))

        if not go_number or not arrival_date:
            return render_template_string(TEMPLATE_ADD, app_title=APP_TITLE, error="GO number and Arrival date are required.")

        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO work_items (doc_id, go_number, translators, deputy_director, typist, arrival_date, submission_date)
                VALUES (NULL, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (go_number, translators, deputy_director, typist, arrival_date, submission_date),
            )
            new_id = cur.fetchone().id
            doc_id = f"DOC-{new_id:05d}"
            cur.execute("UPDATE work_items SET doc_id=%s WHERE id=%s", (doc_id, new_id))
            db.commit()
        return redirect(url_for("index"))

    return render_template_string(TEMPLATE_ADD, app_title=APP_TITLE)


@app.route("/edit/<int:item_id>", methods=["GET", "POST"])
def edit(item_id: int):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT *, TO_CHAR(arrival_date, 'YYYY-MM-DD') AS arrival_date_s, TO_CHAR(submission_date, 'YYYY-MM-DD') AS submission_date_s FROM work_items WHERE id=%s", (item_id,))
        row = cur.fetchone()

    if not row:
        return redirect(url_for("index"))

    if request.method == "POST":
        go_number = request.form.get("go_number", "").strip()
        translators = request.form.get("translators", "").strip()
        deputy_director = request.form.get("deputy_director", "").strip()
        typist = request.form.get("typist", "").strip()
        arrival_date = normalize_date(request.form.get("arrival_date"))
        submission_date = normalize_date(request.form.get("submission_date"))

        with db.cursor() as cur:
            cur.execute(
                """
                UPDATE work_items
                   SET go_number=%s, translators=%s, deputy_director=%s, typist=%s, arrival_date=%s, submission_date=%s
                 WHERE id=%s
                """,
                (go_number, translators, deputy_director, typist, arrival_date, submission_date, item_id),
            )
            db.commit()
        return redirect(url_for("index"))

    # Adapt for template values
    class RowWrap:
        def __init__(self, r):
            self.id = r.id
            self.doc_id = r.doc_id
            self.go_number = r.go_number
            self.translators = r.translators
            self.deputy_director = r.deputy_director
            self.typist = r.typist
            self.arrival_date = r.arrival_date_s
            self.submission_date = r.submission_date_s

    return render_template_string(TEMPLATE_EDIT, app_title=APP_TITLE, row=RowWrap(row))


@app.route("/mark_submitted/<int:item_id>", methods=["POST"])
def mark_submitted(item_id: int):
    today = datetime.now().strftime("%Y-%m-%d")
    db = get_db()
    with db.cursor() as cur:
        cur.execute("UPDATE work_items SET submission_date=%s WHERE id=%s", (today, item_id))
        db.commit()
    return redirect(url_for("index"))


@app.route("/delete/<int:item_id>", methods=["POST"])
def delete(item_id: int):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("DELETE FROM work_items WHERE id=%s", (item_id,))
        db.commit()
    return redirect(url_for("index"))


@app.route("/export.csv")
def export_csv():
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT doc_id, go_number, translators, deputy_director, typist,
                   TO_CHAR(arrival_date, 'YYYY-MM-DD') AS arrival_date,
                   COALESCE(TO_CHAR(submission_date, 'YYYY-MM-DD'), '') AS submission_date
              FROM work_items
             ORDER BY id
            """
        )
        rows = cur.fetchall()

    tmp = Path("/tmp/translation-tracker.csv")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Doc ID", "GO Number", "Translators", "Deputy Director", "Typist", "Arrival Date", "Submission Date",
        ])
        for r in rows:
            writer.writerow([r.doc_id, r.go_number, r.translators, r.deputy_director, r.typist, r.arrival_date, r.submission_date])

    return send_file(tmp, as_attachment=True, download_name="translation-tracker.csv")


# --------------------- Templates ---------------------

BASE_HTML = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ app_title }}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
      body { padding-top: 24px; }
      .container { max-width: 1100px; }
      .badge-inprogress { background-color: #6c757d; }
      .badge-submitted { background-color: #198754; }
      .table td, .table th { vertical-align: middle; }
      .nowrap { white-space: nowrap; }
    </style>
  </head>
  <body>
    <div class="container">
      <nav class="navbar navbar-expand-lg navbar-light mb-3">
        <a class="navbar-brand" href="{{ url_for('index') }}">{{ app_title }}</a>
        <div class="ms-auto">
          <a href="{{ url_for('add') }}" class="btn btn-primary">+ New Entry</a>
          <a href="{{ url_for('export_csv') }}" class="btn btn-outline-secondary">Export CSV</a>
        </div>
      </nav>
      {% block content %}{% endblock %}
    </div>
  </body>
</html>
"""

TEMPLATE_INDEX = """
{% extends none %}{{ BASE_HTML }}{% block content %}
  <form class="row g-2 mb-3" method="get">
    <div class="col-sm-6 col-md-7">
      <input type="text" name="q" value="{{ q }}" class="form-control" placeholder="Search (Doc ID, GO Number, Names)...">
    </div>
    <div class="col-sm-3 col-md-3">
      <select name="status" class="form-select">
        <option value="all" {% if status=='all' %}selected{% endif %}>All</option>
        <option value="inprogress" {% if status=='inprogress' %}selected{% endif %}>In Progress</option>
        <option value="submitted" {% if status=='submitted' %}selected{% endif %}>Submitted</option>
      </select>
    </div>
    <div class="col-sm-3 col-md-2 d-grid">
      <button class="btn btn-outline-primary" type="submit">Filter</button>
    </div>
  </form>

  <div class="table-responsive">
    <table class="table table-striped align-middle">
      <thead>
        <tr>
          <th>Doc ID</th>
          <th>GO Number</th>
          <th>Translators</th>
          <th>Deputy Director</th>
          <th>Typist</th>
          <th class="nowrap">Arrival</th>
          <th class="nowrap">Submission</th>
          <th>Status</th>
          <th class="text-end">Actions</th>
        </tr>
      </thead>
      <tbody>
        {% for r in rows %}
        <tr>
          <td class="fw-semibold">{{ r.doc_id }}</td>
          <td>{{ r.go_number }}</td>
          <td>{{ r.translators }}</td>
          <td>{{ r.deputy_director }}</td>
          <td>{{ r.typist }}</td>
          <td class="nowrap">{{ r.arrival_date }}</td>
          <td class="nowrap">{{ r.submission_date or '' }}</td>
          <td>
            {% if r.status == 'In Progress' %}
              <span class="badge badge-inprogress">In Progress</span>
            {% else %}
              <span class="badge badge-submitted">Submitted</span>
            {% endif %}
          </td>
          <td class="text-end">
            <a class="btn btn-sm btn-outline-secondary" href="{{ url_for('edit', item_id=r.id) }}">Edit</a>
            {% if r.submission_date is none %}
            <form method="post" action="{{ url_for('mark_submitted', item_id=r.id) }}" class="d-inline">
              <button class="btn btn-sm btn-success" type="submit">Mark Submitted</button>
            </form>
            {% endif %}
            <form method="post" action="{{ url_for('delete', item_id=r.id) }}" class="d-inline" onsubmit="return confirm('Delete this entry?');">
              <button class="btn btn-sm btn-outline-danger" type="submit">Delete</button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
{% endblock %}
"""

TEMPLATE_ADD = """
{% extends none %}{{ BASE_HTML }}{% block content %}
  <div class="card">
    <div class="card-body">
      <h5 class="card-title mb-3">New Translation Entry</h5>
      {% if error %}<div class="alert alert-danger">{{ error }}</div>{% endif %}
      <form method="post" class="row g-3">
        <div class="col-md-6">
          <label class="form-label">GO Number *</label>
          <input name="go_number" class="form-control" required>
        </div>
        <div class="col-md-6">
          <label class="form-label">Translators (comma separated)</label>
          <input name="translators" class="form-control" placeholder="e.g., A. Kumar, S. Rao">
        </div>
        <div class="col-md-6">
          <label class="form-label">Deputy Director</label>
          <input name="deputy_director" class="form-control">
        </div>
        <div class="col-md-6">
          <label class="form-label">Typist</label>
          <input name="typist" class="form-control">
        </div>
        <div class="col-md-6">
          <label class="form-label">Arrival Date *</label>
          <input type="date" name="arrival_date" class="form-control" required>
        </div>
        <div class="col-md-6">
          <label class="form-label">Submission Date</label>
          <input type="date" name="submission_date" class="form-control">
        </div>
        <div class="col-12 d-grid d-md-flex gap-2">
          <button class="btn btn-primary" type="submit">Save</button>
          <a class="btn btn-outline-secondary" href="{{ url_for('index') }}">Cancel</a>
        </div>
      </form>
    </div>
  </div>
{% endblock %}
"""

TEMPLATE_EDIT = """
{% extends none %}{{ BASE_HTML }}{% block content %}
  <div class="card">
    <div class="card-body">
      <h5 class="card-title mb-3">Edit Entry — {{ row.doc_id }}</h5>
      <form method="post" class="row g-3">
        <div class="col-md-6">
          <label class="form-label">GO Number *</label>
          <input name="go_number" value="{{ row.go_number }}" class="form-control" required>
        </div>
        <div class="col-md-6">
          <label class="form-label">Translators (comma separated)</label>
          <input name="translators" value="{{ row.translators }}" class="form-control">
        </div>
        <div class="col-md-6">
          <label class="form-label">Deputy Director</label>
          <input name="deputy_director" value="{{ row.deputy_director }}" class="form-control">
        </div>
        <div class="col-md-6">
          <label class="form-label">Typist</label>
          <input name="typist" value="{{ row.typist }}" class="form-control">
        </div>
        <div class="col-md-6">
          <label class="form-label">Arrival Date *</label>
          <input type="date" name="arrival_date" value="{{ row.arrival_date }}" class="form-control" required>
        </div>
        <div class="col-md-6">
          <label class="form-label">Submission Date</label>
          <input type="date" name="submission_date" value="{{ row.submission_date or '' }}" class="form-control">
        </div>
        <div class="col-12 d-grid d-md-flex gap-2">
          <button class="btn btn-primary" type="submit">Save Changes</button>
          <a class="btn btn-outline-secondary" href="{{ url_for('index') }}">Back</a>
        </div>
      </form>
    </div>
  </div>
{% endblock %}
"""

# Jinja needs BASE_HTML in context when using render_template_string and "extends none"
@app.context_processor
def inject_base():
    return {"BASE_HTML": BASE_HTML}

# No app.run() here — Vercel imports `app` via api/index.py
