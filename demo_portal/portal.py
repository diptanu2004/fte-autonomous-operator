"""Acme Corp internal portal — a stand-in for a real company system.

Server-rendered HTML forms on SQLite, deliberately "enterprise-y": strict
validation, pagination, an occasional transient 503 and session expiry, so the
operator agent has to observe and recover rather than follow a happy path.

Run standalone:  python -m demo_portal.portal   (http://127.0.0.1:5055)
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, g, jsonify, redirect, render_template_string, request, session, url_for

DB_PATH = Path(os.environ.get("PORTAL_DB", Path(__file__).with_name("portal.db")))
USERS = {"ops.agent": "Acme!2026", "admin": "admin"}
SESSION_MINUTES = float(os.environ.get("PORTAL_SESSION_MINUTES", "8"))
FLAKY = os.environ.get("PORTAL_FLAKY", "1") == "1"
PAGE_SIZE = 8
INVOICE_STATUSES = ["Approved for payment", "On hold", "Rejected"]
DEPARTMENTS = ["Analytics", "Engineering", "Finance", "IT", "Marketing", "Operations"]
LOCATIONS = ["Bangalore", "New York", "London", "Remote"]

app = Flask(__name__)
app.secret_key = os.environ.get("PORTAL_SECRET", "acme-dev-secret")
_flaky_state = {"invoice_503_sent": False}
_lock = threading.Lock()

# --------------------------------------------------------------------------- data

SEED_VENDORS = [
    ("V-100", "Northwind Traders", "ar@northwind.example"),
    ("V-200", "Contoso Supplies", "billing@contoso.example"),
    ("V-300", "Fabrikam Inc", "accounts@fabrikam.example"),
    ("V-400", "Litware Ltd", "finance@litware.example"),
]
SEED_POS = [
    ("PO-1001", "V-400", "Annual software maintenance", 980.00, "Open"),
    ("PO-1002", "V-100", "Pantry supplies Q3", 410.00, "Closed"),
    ("PO-1003", "V-100", "Ergonomic office chairs x8", 1240.00, "Open"),
    ("PO-1004", "V-300", "Trade-show booth design", 5400.00, "Closed"),
    ("PO-1005", "V-200", "Printer toner", 260.00, "Open"),
    ("PO-1006", "V-400", "Cloud backup licences", 1890.00, "Open"),
    ("PO-1007", "V-200", "Laptops x5 + docking stations", 7850.00, "Open"),
    ("PO-1008", "V-100", "Conference room AV kit", 3300.00, "Open"),
    ("PO-1009", "V-300", "Brand photography", 2150.00, "Closed"),
    ("PO-1010", "V-200", "Monitors x10", 2990.00, "Open"),
    ("PO-1011", "V-300", "Marketing brochures print run", 3200.00, "Open"),
    ("PO-1012", "V-400", "Security awareness training", 1500.00, "Open"),
]
SEED_EMPLOYEES = [
    ("E-001", "Maria Gomez", "maria.gomez@acme-corp.com", "", "Finance Manager", "Finance", "", "2019-04-01", "New York"),
    ("E-002", "Dana Lee", "dana.lee@acme-corp.com", "", "Analytics Manager", "Analytics", "", "2020-02-17", "Bangalore"),
    ("E-003", "Raj Patel", "raj.patel@acme-corp.com", "", "IT Administrator", "IT", "", "2021-06-07", "Bangalore"),
    ("E-004", "Tom Becker", "tom.becker@acme-corp.com", "", "Senior Engineer", "Engineering", "E-003", "2022-09-12", "London"),
]
SEED_INVOICES = [
    ("INV-1990", "V-400", "PO-1001", "2026-09-30", 980.00, "Approved for payment", "Matched PO"),
]


def db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def _close(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def reset_db(path: Path = DB_PATH) -> None:
    """Recreate the portal database with seed data."""
    path.unlink(missing_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE vendors(id TEXT PRIMARY KEY, name TEXT, email TEXT);
        CREATE TABLE purchase_orders(po TEXT PRIMARY KEY, vendor_id TEXT, description TEXT, amount REAL, status TEXT);
        CREATE TABLE invoices(invoice_number TEXT PRIMARY KEY, vendor_id TEXT, po TEXT, invoice_date TEXT,
                              amount REAL, status TEXT, notes TEXT, entered_by TEXT, entered_at TEXT);
        CREATE TABLE employees(id TEXT PRIMARY KEY, name TEXT, email TEXT, personal_email TEXT, title TEXT,
                               department TEXT, manager_id TEXT, start_date TEXT, location TEXT);
        CREATE TABLE tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, employee_id TEXT, item TEXT,
                             priority TEXT, notes TEXT, status TEXT, created_at TEXT);
        """
    )
    conn.executemany("INSERT INTO vendors VALUES (?,?,?)", SEED_VENDORS)
    conn.executemany("INSERT INTO purchase_orders VALUES (?,?,?,?,?)", SEED_POS)
    conn.executemany("INSERT INTO employees VALUES (?,?,?,?,?,?,?,?,?)", SEED_EMPLOYEES)
    conn.executemany(
        "INSERT INTO invoices VALUES (?,?,?,?,?,?,?,'admin','2026-10-01 09:00')", SEED_INVOICES
    )
    conn.commit()
    conn.close()
    _flaky_state["invoice_503_sent"] = False


def ensure_db() -> None:
    if not DB_PATH.exists():
        reset_db()


# --------------------------------------------------------------------------- layout

BASE = """
<!doctype html><html><head><meta charset="utf-8"><title>{{ title }} · Acme Portal</title>
<style>
 body{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#f4f5f7;color:#222}
 header{background:#1f3a5f;color:#fff;padding:10px 20px;display:flex;gap:18px;align-items:center}
 header a{color:#dfe8f5;text-decoration:none} header strong{margin-right:20px}
 main{padding:20px;max-width:1000px} table{border-collapse:collapse;width:100%;background:#fff}
 td,th{border:1px solid #ddd;padding:6px 8px;text-align:left} th{background:#e9edf3}
 .flash{padding:10px;margin-bottom:12px;border-radius:4px}
 .error{background:#fde2e1;border:1px solid #f5a3a0} .ok{background:#e2f6e5;border:1px solid #9ad3a3}
 form.grid{display:grid;grid-template-columns:200px 320px;gap:8px;background:#fff;padding:16px}
 button{padding:6px 14px}
</style></head><body>
<header><strong>Acme Corp Portal</strong>
{% if user %}
 <a href="/">Dashboard</a><a href="/purchase-orders">Purchase Orders</a><a href="/invoices">Invoices</a>
 <a href="/vendors">Vendors</a><a href="/employees">Employees</a><a href="/tickets">IT Tickets</a>
 <span style="margin-left:auto">Signed in as {{ user }} · <a href="/logout">Sign out</a></span>
{% endif %}
</header><main>
<h1>{{ title }}</h1>
{% for kind, msg in flashes %}<div class="flash {{ kind }}" role="alert">{{ msg }}</div>{% endfor %}
{{ body|safe }}
</main></body></html>
"""


def page(title: str, body: str, flashes=None, status: int = 200, **ctx):
    inner = render_template_string(body, **ctx)
    flashes = list(flashes or []) + session.pop("_flashes", [])
    return render_template_string(BASE, title=title, body=inner, flashes=flashes, user=session.get("user")), status


def flash(kind: str, msg: str) -> None:
    session.setdefault("_flashes", []).append((kind, msg))
    session.modified = True


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        user = session.get("user")
        if not user:
            return redirect(url_for("login", next=request.full_path))
        if time.time() - session.get("login_at", 0) > SESSION_MINUTES * 60:
            session.clear()
            flash("error", "Your session has expired. Please sign in again.")
            return redirect(url_for("login", next=request.full_path))
        return fn(*a, **kw)

    return wrapper


def options(items, selected=""):
    return "".join(
        f'<option value="{v}"{" selected" if v == selected else ""}>{label}</option>' for v, label in items
    )


# --------------------------------------------------------------------------- auth

@app.route("/login", methods=["GET", "POST"])
def login():
    errors = []
    if request.method == "POST":
        u, p = request.form.get("username", ""), request.form.get("password", "")
        if USERS.get(u) == p:
            session.clear()
            session["user"], session["login_at"] = u, time.time()
            nxt = request.args.get("next") or "/"
            return redirect(nxt if nxt.startswith("/") else "/")
        errors.append(("error", "Invalid username or password."))
    body = """
    <form method="post" class="grid">
      <label for="username">Username</label><input id="username" name="username" autocomplete="off">
      <label for="password">Password</label><input id="password" name="password" type="password">
      <span></span><button type="submit">Sign in</button>
    </form>"""
    return page("Sign in", body, errors)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    c = db()
    stats = {
        "open_pos": c.execute("SELECT COUNT(*) FROM purchase_orders WHERE status='Open'").fetchone()[0],
        "invoices": c.execute("SELECT COUNT(*) FROM invoices").fetchone()[0],
        "on_hold": c.execute("SELECT COUNT(*) FROM invoices WHERE status='On hold'").fetchone()[0],
        "tickets": c.execute("SELECT COUNT(*) FROM tickets WHERE status='Open'").fetchone()[0],
    }
    body = """<ul><li>Open purchase orders: {{ s.open_pos }}</li><li>Invoices recorded: {{ s.invoices }}</li>
    <li>Invoices on hold: {{ s.on_hold }}</li><li>Open IT tickets: {{ s.tickets }}</li></ul>"""
    return page("Dashboard", body, s=stats)


# --------------------------------------------------------------------------- procurement

@app.route("/vendors")
@login_required
def vendors():
    rows = db().execute("SELECT * FROM vendors ORDER BY id").fetchall()
    body = """<table><tr><th>Vendor ID</th><th>Name</th><th>Billing contact email</th></tr>
    {% for r in rows %}<tr><td>{{ r.id }}</td><td>{{ r.name }}</td><td>{{ r.email }}</td></tr>{% endfor %}</table>"""
    return page("Vendors", body, rows=rows)


@app.route("/purchase-orders")
@login_required
def purchase_orders():
    q = request.args.get("q", "").strip()
    pg = max(1, int(request.args.get("page", "1") or 1))
    sql = """SELECT p.*, v.name vendor FROM purchase_orders p JOIN vendors v ON v.id=p.vendor_id
             WHERE p.po LIKE ? OR v.name LIKE ? OR p.description LIKE ? ORDER BY p.po"""
    rows = db().execute(sql, (f"%{q}%",) * 3).fetchall()
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    rows = rows[(pg - 1) * PAGE_SIZE : pg * PAGE_SIZE]
    body = """
    <form method="get"><label for="q">Search POs</label> <input id="q" name="q" value="{{ q }}">
      <button type="submit">Search</button></form><br>
    <table><tr><th>PO number</th><th>Vendor</th><th>Description</th><th>Amount (USD)</th><th>Status</th></tr>
    {% for r in rows %}<tr><td><a href="/purchase-orders/{{ r.po }}">{{ r.po }}</a></td><td>{{ r.vendor }}</td>
      <td>{{ r.description }}</td><td>{{ '%.2f'|format(r.amount) }}</td><td>{{ r.status }}</td></tr>{% endfor %}
    </table><p>Page {{ pg }} of {{ pages }}
    {% if pg < pages %} · <a href="?q={{ q }}&page={{ pg + 1 }}">Next page</a>{% endif %}
    {% if pg > 1 %} · <a href="?q={{ q }}&page={{ pg - 1 }}">Previous page</a>{% endif %}</p>"""
    return page("Purchase Orders", body, rows=rows, q=q, pg=pg, pages=pages)


@app.route("/purchase-orders/<po>")
@login_required
def purchase_order(po):
    r = db().execute(
        "SELECT p.*, v.name vendor, v.email FROM purchase_orders p JOIN vendors v ON v.id=p.vendor_id WHERE po=?",
        (po,),
    ).fetchone()
    if not r:
        return page("Not found", "<p>No purchase order {{ po }}.</p>", status=404, po=po)
    inv = db().execute("SELECT * FROM invoices WHERE po=?", (po,)).fetchall()
    body = """<table>
    <tr><th>PO number</th><td>{{ r.po }}</td></tr><tr><th>Vendor</th><td>{{ r.vendor }} ({{ r.vendor_id }})</td></tr>
    <tr><th>Description</th><td>{{ r.description }}</td></tr>
    <tr><th>Approved amount (USD)</th><td>{{ '%.2f'|format(r.amount) }}</td></tr><tr><th>Status</th><td>{{ r.status }}</td></tr>
    </table><h3>Invoices against this PO</h3>
    {% if inv %}<ul>{% for i in inv %}<li>{{ i.invoice_number }} — {{ '%.2f'|format(i.amount) }} — {{ i.status }}</li>{% endfor %}</ul>
    {% else %}<p>None recorded.</p>{% endif %}"""
    return page(f"Purchase Order {po}", body, r=r, inv=inv)


@app.route("/invoices")
@login_required
def invoices():
    rows = db().execute(
        "SELECT i.*, v.name vendor FROM invoices i JOIN vendors v ON v.id=i.vendor_id ORDER BY entered_at DESC"
    ).fetchall()
    body = """<p><a href="/invoices/new">Record new invoice</a></p>
    <table><tr><th>Invoice #</th><th>Vendor</th><th>PO</th><th>Date</th><th>Amount (USD)</th><th>Status</th><th>Notes</th></tr>
    {% for r in rows %}<tr><td>{{ r.invoice_number }}</td><td>{{ r.vendor }}</td><td>{{ r.po }}</td><td>{{ r.invoice_date }}</td>
    <td>{{ '%.2f'|format(r.amount) }}</td><td>{{ r.status }}</td><td>{{ r.notes }}</td></tr>{% endfor %}</table>"""
    return page("Invoices", body, rows=rows)


@app.route("/invoices/new", methods=["GET", "POST"])
@login_required
def invoice_new():
    c = db()
    form = request.form if request.method == "POST" else {}
    errors = []
    if request.method == "POST":
        with _lock:
            if FLAKY and not _flaky_state["invoice_503_sent"]:
                _flaky_state["invoice_503_sent"] = True
                return page(
                    "Service unavailable",
                    "<p>The invoice service is temporarily unavailable (503). Nothing was saved. Please try again.</p>"
                    '<p><a href="/invoices/new">Back to invoice form</a></p>',
                    status=503,
                )
        num = form.get("invoice_number", "").strip()
        vendor_id = form.get("vendor_id", "")
        po = form.get("po_number", "").strip().upper()
        date = form.get("invoice_date", "").strip()
        amount_raw = form.get("amount", "").strip()
        status = form.get("status", "")
        notes = form.get("notes", "").strip()
        if not re.fullmatch(r"INV-\d{3,6}", num):
            errors.append("Invoice number must look like INV-1234.")
        elif c.execute("SELECT 1 FROM invoices WHERE invoice_number=?", (num,)).fetchone():
            errors.append(f"Invoice {num} has already been recorded (duplicate).")
        po_row = c.execute("SELECT * FROM purchase_orders WHERE po=?", (po,)).fetchone()
        if not po_row:
            errors.append(f"PO number '{po}' does not exist.")
        elif po_row["vendor_id"] != vendor_id:
            errors.append(f"PO {po} belongs to a different vendor than the one selected.")
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            errors.append("Invoice date must be in YYYY-MM-DD format.")
        try:
            if not re.fullmatch(r"\d+(\.\d{1,2})?", amount_raw):
                raise ValueError
            amount = float(amount_raw)
        except ValueError:
            errors.append("Amount must be a plain number such as 1234.50 (no currency symbols or commas).")
            amount = None
        if status not in INVOICE_STATUSES:
            errors.append("Select a status.")
        if status in ("On hold", "Rejected") and not notes:
            errors.append("Notes are required when an invoice is put on hold or rejected.")
        if not errors:
            c.execute(
                "INSERT INTO invoices VALUES (?,?,?,?,?,?,?,?,?)",
                (num, vendor_id, po, date, amount, status, notes, session["user"], datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            c.commit()
            flash("ok", f"Invoice {num} saved with status '{status}'.")
            return redirect(url_for("invoices"))
    vendors_ = [(r["id"], f'{r["name"]} ({r["id"]})') for r in c.execute("SELECT * FROM vendors ORDER BY name")]
    body = """
    <form method="post" class="grid">
      <label for="invoice_number">Invoice number</label><input id="invoice_number" name="invoice_number" value="{{ f.get('invoice_number','') }}">
      <label for="vendor_id">Vendor</label><select id="vendor_id" name="vendor_id"><option value="">-- choose --</option>{{ vopts|safe }}</select>
      <label for="po_number">PO number</label><input id="po_number" name="po_number" value="{{ f.get('po_number','') }}">
      <label for="invoice_date">Invoice date</label><input id="invoice_date" name="invoice_date" placeholder="YYYY-MM-DD" value="{{ f.get('invoice_date','') }}">
      <label for="amount">Amount (USD)</label><input id="amount" name="amount" value="{{ f.get('amount','') }}">
      <label for="status">Status</label><select id="status" name="status"><option value="">-- choose --</option>{{ sopts|safe }}</select>
      <label for="notes">Notes</label><textarea id="notes" name="notes">{{ f.get('notes','') }}</textarea>
      <span></span><button type="submit">Save invoice</button>
    </form>"""
    return page(
        "Record Invoice",
        body,
        [("error", e) for e in errors],
        status=400 if errors else 200,
        f=form,
        vopts=options(vendors_, form.get("vendor_id", "")),
        sopts=options([(s, s) for s in INVOICE_STATUSES], form.get("status", "")),
    )


# --------------------------------------------------------------------------- HR / IT

@app.route("/employees")
@login_required
def employees():
    rows = db().execute(
        "SELECT e.*, m.name manager FROM employees e LEFT JOIN employees m ON m.id=e.manager_id ORDER BY e.id"
    ).fetchall()
    body = """<p><a href="/employees/new">Add employee</a></p>
    <table><tr><th>ID</th><th>Name</th><th>Work email</th><th>Title</th><th>Department</th><th>Manager</th><th>Start date</th><th>Location</th></tr>
    {% for r in rows %}<tr><td>{{ r.id }}</td><td>{{ r.name }}</td><td>{{ r.email }}</td><td>{{ r.title }}</td><td>{{ r.department }}</td>
    <td>{{ r.manager or '' }}</td><td>{{ r.start_date }}</td><td>{{ r.location }}</td></tr>{% endfor %}</table>"""
    return page("Employees", body, rows=rows)


@app.route("/employees/new", methods=["GET", "POST"])
@login_required
def employee_new():
    c = db()
    form = request.form if request.method == "POST" else {}
    errors = []
    if request.method == "POST":
        name = form.get("name", "").strip()
        email = form.get("email", "").strip().lower()
        personal = form.get("personal_email", "").strip()
        start = form.get("start_date", "").strip()
        if len(name.split()) < 2:
            errors.append("Full name (first and last) is required.")
        if not re.fullmatch(r"[a-z]+\.[a-z]+@acme-corp\.com", email):
            errors.append("Work email must follow firstname.lastname@acme-corp.com.")
        elif c.execute("SELECT 1 FROM employees WHERE email=?", (email,)).fetchone():
            errors.append("An employee with this work email already exists.")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", personal):
            errors.append("A valid personal email is required (used to send pre-start credentials).")
        for field, label in (("title", "Job title"), ("department", "Department"), ("manager_id", "Manager"), ("location", "Location")):
            if not form.get(field, "").strip():
                errors.append(f"{label} is required.")
        try:
            datetime.strptime(start, "%Y-%m-%d")
        except ValueError:
            errors.append("Start date must be in YYYY-MM-DD format.")
        if not errors:
            n = c.execute("SELECT COUNT(*) FROM employees").fetchone()[0] + 1
            eid = f"E-{n:03d}"
            c.execute(
                "INSERT INTO employees VALUES (?,?,?,?,?,?,?,?,?)",
                (eid, name, email, personal, form["title"].strip(), form["department"], form["manager_id"], start, form["location"]),
            )
            c.commit()
            flash("ok", f"Employee {name} created with ID {eid}.")
            return redirect(url_for("employees"))
    mgrs = [(r["id"], f'{r["name"]} ({r["id"]})') for r in c.execute("SELECT * FROM employees ORDER BY name")]
    body = """
    <form method="post" class="grid">
      <label for="name">Full name</label><input id="name" name="name" value="{{ f.get('name','') }}">
      <label for="email">Work email</label><input id="email" name="email" value="{{ f.get('email','') }}">
      <label for="personal_email">Personal email</label><input id="personal_email" name="personal_email" value="{{ f.get('personal_email','') }}">
      <label for="title">Job title</label><input id="title" name="title" value="{{ f.get('title','') }}">
      <label for="department">Department</label><select id="department" name="department"><option value="">-- choose --</option>{{ dopts|safe }}</select>
      <label for="manager_id">Manager</label><select id="manager_id" name="manager_id"><option value="">-- choose --</option>{{ mopts|safe }}</select>
      <label for="start_date">Start date</label><input id="start_date" name="start_date" placeholder="YYYY-MM-DD" value="{{ f.get('start_date','') }}">
      <label for="location">Location</label><select id="location" name="location"><option value="">-- choose --</option>{{ lopts|safe }}</select>
      <span></span><button type="submit">Create employee</button>
    </form>"""
    return page(
        "Add Employee",
        body,
        [("error", e) for e in errors],
        status=400 if errors else 200,
        f=form,
        dopts=options([(d, d) for d in DEPARTMENTS], form.get("department", "")),
        mopts=options(mgrs, form.get("manager_id", "")),
        lopts=options([(l, l) for l in LOCATIONS], form.get("location", "")),
    )


@app.route("/tickets")
@login_required
def tickets():
    rows = db().execute(
        "SELECT t.*, e.name employee FROM tickets t LEFT JOIN employees e ON e.id=t.employee_id ORDER BY t.id"
    ).fetchall()
    body = """<p><a href="/tickets/new">Open new ticket</a></p>
    <table><tr><th>#</th><th>Category</th><th>For employee</th><th>Item</th><th>Priority</th><th>Status</th><th>Notes</th></tr>
    {% for r in rows %}<tr><td>{{ r.id }}</td><td>{{ r.category }}</td><td>{{ r.employee }}</td><td>{{ r.item }}</td>
    <td>{{ r.priority }}</td><td>{{ r.status }}</td><td>{{ r.notes }}</td></tr>{% endfor %}</table>"""
    return page("IT Tickets", body, rows=rows)


@app.route("/tickets/new", methods=["GET", "POST"])
@login_required
def ticket_new():
    c = db()
    form = request.form if request.method == "POST" else {}
    errors = []
    if request.method == "POST":
        if form.get("category") not in ("Hardware", "Software access"):
            errors.append("Select a category.")
        if not c.execute("SELECT 1 FROM employees WHERE id=?", (form.get("employee_id", ""),)).fetchone():
            errors.append("Select the employee this ticket is for.")
        if not form.get("item", "").strip():
            errors.append("Item is required.")
        if form.get("priority") not in ("Low", "Normal", "High"):
            errors.append("Select a priority.")
        if not errors:
            cur = c.execute(
                "INSERT INTO tickets(category, employee_id, item, priority, notes, status, created_at) VALUES (?,?,?,?,?,'Open',?)",
                (form["category"], form["employee_id"], form["item"].strip(), form["priority"], form.get("notes", "").strip(),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            c.commit()
            flash("ok", f"Ticket #{cur.lastrowid} opened.")
            return redirect(url_for("tickets"))
    emps = [(r["id"], f'{r["name"]} ({r["id"]})') for r in c.execute("SELECT * FROM employees ORDER BY name")]
    body = """
    <form method="post" class="grid">
      <label for="category">Category</label><select id="category" name="category"><option value="">-- choose --</option>{{ copts|safe }}</select>
      <label for="employee_id">For employee</label><select id="employee_id" name="employee_id"><option value="">-- choose --</option>{{ eopts|safe }}</select>
      <label for="item">Item</label><input id="item" name="item" value="{{ f.get('item','') }}">
      <label for="priority">Priority</label><select id="priority" name="priority"><option value="">-- choose --</option>{{ popts|safe }}</select>
      <label for="notes">Notes</label><textarea id="notes" name="notes">{{ f.get('notes','') }}</textarea>
      <span></span><button type="submit">Open ticket</button>
    </form>"""
    return page(
        "Open IT Ticket",
        body,
        [("error", e) for e in errors],
        status=400 if errors else 200,
        f=form,
        copts=options([(x, x) for x in ("Hardware", "Software access")], form.get("category", "")),
        eopts=options(emps, form.get("employee_id", "")),
        popts=options([(x, x) for x in ("Low", "Normal", "High")], form.get("priority", "")),
    )


# --------------------------------------------------------------------------- test hooks (not linked in UI)

@app.route("/__state")
def state_dump():
    c = db()
    return jsonify({t: [dict(r) for r in c.execute(f"SELECT * FROM {t}")] for t in ("invoices", "employees", "tickets")})


@app.route("/__reset", methods=["POST"])
def state_reset():
    reset_db()
    return jsonify(ok=True)


# --------------------------------------------------------------------------- serving

def serve(port: int = 5055) -> None:
    import logging

    ensure_db()
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    from werkzeug.serving import make_server

    make_server("127.0.0.1", port, app, threaded=True).serve_forever()


def serve_in_background(port: int = 5055) -> bool:
    """Start the portal in a daemon thread unless something already listens on the port."""
    import socket

    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return False
    threading.Thread(target=serve, args=(port,), daemon=True, name="acme-portal").start()
    for _ in range(50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return True


if __name__ == "__main__":
    ensure_db()
    print("Acme portal on http://127.0.0.1:5055  (user ops.agent / Acme!2026)")
    serve()
