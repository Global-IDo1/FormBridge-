import base64
import json
import os
import re
import secrets
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DB_PATH = os.environ.get("FORMBRIDGE_DB", "formbridge.db")
SESSIONS = {}
UPLOAD_DIR = os.environ.get("FORMBRIDGE_UPLOAD_DIR", os.environ.get("UPLOAD_DIR", "uploads"))
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "FormBridge <no-reply@formbridge.in>")
CASHFREE_APP_ID = os.environ.get("CASHFREE_APP_ID", "")
CASHFREE_SECRET_KEY = os.environ.get("CASHFREE_SECRET_KEY", "")
CASHFREE_ENV = os.environ.get("CASHFREE_ENV", "sandbox").lower()
CASHFREE_BASE_URL = os.environ.get("CASHFREE_BASE_URL", "").strip()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def utc_today():
    return datetime.now(timezone.utc).date().isoformat()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table, column, definition):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    conn = get_db()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('student','operator','admin')),
            is_complete INTEGER DEFAULT 0,
            is_approved INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS student_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL UNIQUE,
            full_name TEXT,dob TEXT,phone TEXT,address TEXT,education TEXT,photo_url TEXT,signature_url TEXT,
            FOREIGN KEY(profile_id) REFERENCES profiles(id)
        );

        CREATE TABLE IF NOT EXISTS exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            short_name TEXT NOT NULL,
            full_name TEXT NOT NULL,
            official_url TEXT NOT NULL,
            is_active INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT UNIQUE NOT NULL,
            student_id INTEGER NOT NULL,
            exam_id INTEGER NOT NULL,
            operator_id INTEGER,
            status TEXT NOT NULL,
            received_at TEXT,
            assigned_at TEXT,
            submitted_at TEXT,
            proof_url TEXT,
            application_number TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(student_id) REFERENCES profiles(id),
            FOREIGN KEY(operator_id) REFERENCES profiles(id),
            FOREIGN KEY(exam_id) REFERENCES exams(id)
        );

        CREATE TABLE IF NOT EXISTS order_status_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            changed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS operator_stats (
            operator_id INTEGER PRIMARY KEY,
            accuracy_score REAL DEFAULT 0,
            current_streak INTEGER DEFAULT 0,
            total_completed INTEGER DEFAULT 0,
            last_active_date TEXT
        );

        CREATE TABLE IF NOT EXISTS email_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            to_email TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            provider_status TEXT
        );

        CREATE TABLE IF NOT EXISTS cashfree_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cashfree_order_id TEXT UNIQUE NOT NULL,
            session_id TEXT,
            payment_id TEXT,
            payment_status TEXT,
            verified_at TEXT,
            student_id INTEGER NOT NULL,
            exam_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    ensure_column(conn, "order_status_log", "changed_at", "TEXT")
    if "created_at" in [r["name"] for r in conn.execute("PRAGMA table_info(order_status_log)").fetchall()]:
        conn.execute("UPDATE order_status_log SET changed_at = COALESCE(changed_at, created_at)")

    cur.execute(
        "INSERT OR IGNORE INTO profiles (email,password,role,is_complete,is_approved,created_at) VALUES (?,?,?,?,?,?)",
        ("admin@formbridge.in", "adminpass", "admin", 1, 1, now_iso()),
    )
    cur.execute(
        "INSERT OR IGNORE INTO exams (id,short_name,full_name,official_url,is_active) VALUES (1,'SSC CGL','Combined Graduate Level','https://ssc.gov.in',1)"
    )
    cur.execute(
        "INSERT OR IGNORE INTO exams (id,short_name,full_name,official_url,is_active) VALUES (2,'RRB NTPC','Railway NTPC','https://www.rrbcdg.gov.in',1)"
    )
    conn.commit()
    conn.close()


def json_response(handler, code, payload):
    body = json.dumps(payload).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def html_response(handler, code, html):
    body = html.encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def parse_body(handler):
    length = int(handler.headers.get("Content-Length", 0))
    raw = handler.rfile.read(length) if length else b"{}"
    ctype = handler.headers.get("Content-Type", "")
    if "application/json" in ctype:
        return json.loads(raw.decode() or "{}")
    return {k: v[0] for k, v in parse_qs(raw.decode()).items()}


def get_session_user(handler):
    cookie = cookies.SimpleCookie(handler.headers.get("Cookie"))
    sid = cookie.get("sid")
    if not sid:
        return None
    return SESSIONS.get(sid.value)


def create_session(handler, user):
    sid = secrets.token_hex(16)
    SESSIONS[sid] = user
    handler.send_header("Set-Cookie", f"sid={sid}; Path=/; HttpOnly")


def log_email(to_email, subject, body, provider_status):
    conn = get_db()
    conn.execute(
        "INSERT INTO email_log (to_email,subject,body,created_at,provider_status) VALUES (?,?,?,?,?)",
        (to_email, subject, body, now_iso(), provider_status),
    )
    conn.commit()
    conn.close()


def call_resend(to_email, subject, body):
    if not RESEND_API_KEY:
        log_email(to_email, subject, body, "skipped_missing_resend_key")
        return
    payload = {
        "from": RESEND_FROM,
        "to": [to_email],
        "subject": subject,
        "text": body,
    }
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20):
            log_email(to_email, subject, body, "sent")
    except Exception as exc:  # noqa: BLE001
        log_email(to_email, subject, body, f"failed:{exc}")


def generate_order_number(conn):
    year = datetime.now(timezone.utc).year
    for _ in range(20):
        suffix = secrets.randbelow(9000) + 1000
        candidate = f"FB-{year}-{suffix}"
        exists = conn.execute("SELECT 1 FROM orders WHERE order_number=?", (candidate,)).fetchone()
        if not exists:
            return candidate
    raise RuntimeError("Unable to generate unique order number")


def update_order_status(conn, order_id, new_status, extra_fields=None):
    ts = now_iso()
    fields = {"status": new_status, "updated_at": ts}
    if extra_fields:
        fields.update(extra_fields)
    set_clause = ", ".join([f"{k}=?" for k in fields.keys()])
    values = list(fields.values()) + [order_id]
    conn.execute(f"UPDATE orders SET {set_clause} WHERE id=?", values)
    conn.execute(
        "INSERT INTO order_status_log (order_id,status,changed_at) VALUES (?,?,?)",
        (order_id, new_status, ts),
    )


def ensure_profile_row(email, password, role):
    conn = get_db()
    prof = conn.execute("SELECT id,email,role,is_approved FROM profiles WHERE email=?", (email,)).fetchone()
    if not prof:
        conn.execute(
            "INSERT INTO profiles (email,password,role,is_complete,is_approved,created_at) VALUES (?,?,?,?,?,?)",
            (email, password, role, 0, 0 if role == "operator" else 1, now_iso()),
        )
        conn.commit()
        prof = conn.execute("SELECT id,email,role,is_approved FROM profiles WHERE email=?", (email,)).fetchone()
    conn.close()
    return prof


def redirect_response(handler, location):
    handler.send_response(302)
    handler.send_header("Location", location)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def cashfree_base_url():
    if CASHFREE_BASE_URL:
        return CASHFREE_BASE_URL.rstrip("/")
    return "https://sandbox.cashfree.com/pg" if CASHFREE_ENV != "production" else "https://api.cashfree.com/pg"


def cashfree_headers():
    return {
        "x-api-version": "2023-08-01",
        "x-client-id": CASHFREE_APP_ID,
        "x-client-secret": CASHFREE_SECRET_KEY,
        "Content-Type": "application/json",
    }


def create_cashfree_order(student_id, exam_id, amount):
    if not CASHFREE_APP_ID or not CASHFREE_SECRET_KEY:
        raise RuntimeError("Cashfree keys not configured")
    order_ref = f"fb_{student_id}_{int(datetime.now(timezone.utc).timestamp())}_{secrets.randbelow(1000)}"
    payload = {
        "order_id": order_ref,
        "order_amount": amount,
        "order_currency": "INR",
        "customer_details": {
            "customer_id": str(student_id),
            "customer_email": f"student_{student_id}@formbridge.local",
            "customer_phone": "9999999999",
        },
        "order_meta": {"return_url": "https://formbridge.local/payment-callback"},
    }
    req = urllib.request.Request(
        f"{cashfree_base_url()}/orders",
        data=json.dumps(payload).encode(),
        method="POST",
        headers=cashfree_headers(),
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())

    conn = get_db()
    conn.execute(
        "INSERT INTO cashfree_payments (cashfree_order_id,session_id,student_id,exam_id,amount,created_at) VALUES (?,?,?,?,?,?)",
        (data["order_id"], data.get("payment_session_id"), student_id, exam_id, amount, now_iso()),
    )
    conn.commit()
    conn.close()
    return data


def verify_cashfree_payment(cashfree_order_id):
    if not CASHFREE_APP_ID or not CASHFREE_SECRET_KEY:
        raise RuntimeError("Cashfree keys not configured")

    req = urllib.request.Request(
        f"{cashfree_base_url()}/orders/{cashfree_order_id}/payments",
        method="GET",
        headers=cashfree_headers(),
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())

    successful = [p for p in data if p.get("payment_status") == "SUCCESS"]
    if not successful:
        return None
    payment = successful[0]
    conn = get_db()
    conn.execute(
        "UPDATE cashfree_payments SET payment_id=?, payment_status=?, verified_at=? WHERE cashfree_order_id=?",
        (payment.get("cf_payment_id"), "SUCCESS", now_iso(), cashfree_order_id),
    )
    conn.commit()
    conn.close()
    return payment


def assign_operator(order_id):
    conn = get_db()
    order = conn.execute("SELECT exam_id FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return None
    today = utc_today()
    candidates = conn.execute(
        """
        SELECT p.id, p.email, COALESCE(s.accuracy_score,0) accuracy_score, COALESCE(s.current_streak,0) current_streak
        FROM profiles p
        LEFT JOIN operator_stats s ON s.operator_id = p.id
        WHERE p.role='operator' AND p.is_approved=1
          AND NOT EXISTS (
            SELECT 1 FROM orders o
            WHERE o.operator_id = p.id
              AND o.status IN ('ASSIGNED','IN_PROGRESS')
              AND DATE(o.received_at) = DATE(?)
          )
        ORDER BY accuracy_score DESC, current_streak DESC, p.id ASC
        """,
        (today,),
    ).fetchall()

    if not candidates:
        conn.close()
        return None

    operator = candidates[0]
    update_order_status(conn, order_id, "ASSIGNED", {"operator_id": operator["id"], "assigned_at": now_iso()})
    conn.commit()
    conn.close()

    call_resend(
        operator["email"],
        f"New form assignment: Order #{order_id}",
        "New form assignment received. Login to complete it.",
    )
    return operator["id"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        user = get_session_user(self)

        if path.startswith("/uploads/"):
            fp = path.lstrip("/")
            if not os.path.isfile(fp):
                return json_response(self, 404, {"error": "Not found"})
            with open(fp, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/" or path == "/dashboard":
            if not user or user["role"] != "student":
                return html_response(self, 200, "<h1>FormBridge</h1><p>Please login.</p>")
            return html_response(self, 200, "<h1>Student Dashboard</h1><p>Open exams available.</p>")

        if path == "/profile/setup":
            if not user or user["role"] != "student":
                return json_response(self, 401, {"error": "Unauthorized"})
            return html_response(self, 200, "<h1>Profile Setup (5 steps)</h1>")

        if path == "/api/me/profile":
            if not user:
                return json_response(self, 401, {"error": "Unauthorized"})
            conn = get_db()
            row = conn.execute("SELECT id,email,role,is_complete,is_approved FROM profiles WHERE id=?", (user["id"],)).fetchone()
            conn.close()
            return json_response(self, 200, {"profile": dict(row)})

        if path == "/api/exams":
            conn = get_db()
            exams = [dict(r) for r in conn.execute("SELECT * FROM exams WHERE is_active=1 ORDER BY id").fetchall()]
            conn.close()
            return json_response(self, 200, {"exams": exams})

        m = re.match(r"^/order/(\d+)/confirm$", path)
        if m:
            oid = int(m.group(1))
            conn = get_db()
            order = conn.execute("SELECT order_number FROM orders WHERE id=?", (oid,)).fetchone()
            conn.close()
            if not order:
                return json_response(self, 404, {"error": "Not found"})
            return html_response(self, 200, f"<h1>Order Confirmed</h1><p>{order['order_number']}</p><a href='/track/{oid}'>Track</a>")

        m = re.match(r"^/track/(\d+)$", path)
        if m:
            oid = int(m.group(1))
            return html_response(
                self,
                200,
                f"""
            <h1>Track Order {oid}</h1>
            <div id='status'>Loading...</div>
            <script>
            async function poll(){{
              const r = await fetch('/api/track/{oid}');
              const d = await r.json();
              document.getElementById('status').innerText = d.status_text;
            }}
            poll(); setInterval(poll, 30000);
            </script>
            """,
            )

        m = re.match(r"^/api/track/(\d+)$", path)
        if m:
            oid = int(m.group(1))
            conn = get_db()
            order = conn.execute(
                """
                SELECT o.id,o.order_number,o.status,o.received_at,o.assigned_at,o.submitted_at,o.updated_at,
                       o.proof_url,o.application_number,e.short_name,e.full_name
                FROM orders o JOIN exams e ON e.id=o.exam_id WHERE o.id=?
                """,
                (oid,),
            ).fetchone()
            if not order:
                conn.close()
                return json_response(self, 404, {"error": "Not found"})
            history = [
                dict(r)
                for r in conn.execute(
                    "SELECT status,changed_at FROM order_status_log WHERE order_id=? ORDER BY changed_at ASC", (oid,)
                ).fetchall()
            ]
            conn.close()
            status_text = "Finding operator" if order["status"] == "RECEIVED" else order["status"]
            return json_response(
                self,
                200,
                {
                    "order": dict(order),
                    "exam_name": order["full_name"],
                    "status": order["status"],
                    "status_text": status_text,
                    "timeline": history,
                },
            )

        if path == "/operator":
            if not user or user["role"] != "operator":
                return json_response(self, 403, {"error": "Forbidden"})
            conn = get_db()
            prof = conn.execute("SELECT is_approved FROM profiles WHERE id=?", (user["id"],)).fetchone()
            if not prof or not prof["is_approved"]:
                conn.close()
                return html_response(self, 200, "<h1>Account pending approval</h1>")
            assigned = conn.execute(
                "SELECT id, order_number, status FROM orders WHERE operator_id=? AND status IN ('ASSIGNED','IN_PROGRESS') ORDER BY id DESC",
                (user["id"],),
            ).fetchall()
            conn.close()
            return json_response(self, 200, {"assignments": [dict(x) for x in assigned]})

        m = re.match(r"^/operator/orders/(\d+)/open$", path)
        if m:
            if not user or user["role"] != "operator":
                return json_response(self, 403, {"error": "Forbidden"})
            oid = int(m.group(1))
            conn = get_db()
            order = conn.execute("SELECT id,status,operator_id FROM orders WHERE id=?", (oid,)).fetchone()
            if not order or order["operator_id"] != user["id"]:
                conn.close()
                return json_response(self, 403, {"error": "Forbidden"})
            if order["status"] == "ASSIGNED":
                update_order_status(conn, oid, "IN_PROGRESS")
                conn.commit()
            conn.close()
            return json_response(self, 200, {"message": "Fill screen opened", "redirect": f"/operator/orders/{oid}/fill"})

        m = re.match(r"^/operator/orders/(\d+)/fill$", path)
        if m:
            if not user or user["role"] != "operator":
                return json_response(self, 403, {"error": "Forbidden"})
            oid = int(m.group(1))
            conn = get_db()
            row = conn.execute(
                """
                SELECT o.id, o.status, o.operator_id, o.order_number, e.official_url, e.short_name,
                       p.email as student_email,
                       s.full_name, s.dob, s.phone, s.address, s.education, s.photo_url, s.signature_url
                FROM orders o
                JOIN student_details s ON s.profile_id=o.student_id
                JOIN profiles p ON p.id=o.student_id
                JOIN exams e ON e.id=o.exam_id
                WHERE o.id=?
                """,
                (oid,),
            ).fetchone()
            if not row:
                conn.close()
                return json_response(self, 404, {"error": "Not found"})
            if row["operator_id"] != user["id"] or row["status"] not in ("ASSIGNED", "IN_PROGRESS"):
                conn.close()
                return json_response(self, 403, {"error": "Forbidden"})
            if row["status"] == "ASSIGNED":
                update_order_status(conn, oid, "IN_PROGRESS")
                conn.commit()
                row = conn.execute(
                    """
                    SELECT o.id, o.status, o.operator_id, o.order_number, e.official_url, e.short_name,
                           p.email as student_email,
                           s.full_name, s.dob, s.phone, s.address, s.education, s.photo_url, s.signature_url
                    FROM orders o
                    JOIN student_details s ON s.profile_id=o.student_id
                    JOIN profiles p ON p.id=o.student_id
                    JOIN exams e ON e.id=o.exam_id
                    WHERE o.id=?
                    """,
                    (oid,),
                ).fetchone()
            order = dict(row)
            conn.close()
            order["full_name"] = (order.get("full_name") or "").upper()
            dob = order.get("dob")
            if dob and re.match(r"\d{4}-\d{2}-\d{2}", dob):
                y, mth, d = dob.split("-")
                order["dob"] = f"{d}/{mth}/{y}"
            return json_response(self, 200, order)

        if path == "/admin":
            if not user or user["role"] != "admin":
                return redirect_response(self, "/")
            conn = get_db()
            today = utc_today()
            total = conn.execute("SELECT COUNT(*) c FROM orders WHERE DATE(created_at)=DATE(?)", (today,)).fetchone()["c"]
            pending = conn.execute("SELECT COUNT(*) c FROM profiles WHERE role='operator' AND is_approved=0").fetchone()["c"]
            statuses = {r["status"]: r["c"] for r in conn.execute("SELECT status, COUNT(*) c FROM orders GROUP BY status")}
            conn.close()
            return json_response(self, 200, {"total_orders_today": total, "by_status": statuses, "pending_operator_approvals": pending})

        if path == "/admin/operators":
            if not user or user["role"] != "admin":
                return json_response(self, 403, {"error": "Forbidden"})
            conn = get_db()
            ops = [dict(r) for r in conn.execute("SELECT id,email,is_approved FROM profiles WHERE role='operator' ORDER BY id DESC")]
            conn.close()
            return json_response(self, 200, {"operators": ops})

        if path == "/admin/orders":
            if not user or user["role"] != "admin":
                return json_response(self, 403, {"error": "Forbidden"})
            conn = get_db()
            rows = [dict(r) for r in conn.execute("SELECT id,order_number,status,operator_id FROM orders ORDER BY id DESC")]
            conn.close()
            return json_response(self, 200, {"orders": rows})

        if path == "/admin/exams":
            if not user or user["role"] != "admin":
                return json_response(self, 403, {"error": "Forbidden"})
            conn = get_db()
            rows = [dict(r) for r in conn.execute("SELECT * FROM exams ORDER BY id DESC")]
            conn.close()
            return json_response(self, 200, {"exams": rows})

        return json_response(self, 404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = parse_body(self)
        user = get_session_user(self)

        if path == "/api/register":
            role = body.get("role", "student")
            if role not in ("student", "operator"):
                return json_response(self, 400, {"error": "Invalid role"})
            if not body.get("email") or not body.get("password"):
                return json_response(self, 400, {"error": "email and password required"})
            profile = ensure_profile_row(body["email"], body["password"], role)
            if profile["role"] != role:
                return json_response(self, 409, {"error": "Email already exists"})
            if role == "operator":
                conn2 = get_db()
                conn2.execute(
                    "INSERT OR IGNORE INTO operator_stats (operator_id,accuracy_score,current_streak,total_completed,last_active_date) VALUES (?,?,?,?,?)",
                    (profile["id"], 95, 0, 0, None),
                )
                conn2.commit()
                conn2.close()
            self.send_response(200)
            create_session(self, dict(profile))
            self.send_header("Content-Type", "application/json")
            payload = {
                "profile": dict(profile),
                "redirect": "/profile/setup" if role == "student" else "/operator",
                "message": "Account pending approval" if role == "operator" else "Registered",
            }
            raw = json.dumps(payload).encode()
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if path == "/api/login":
            conn = get_db()
            prof = conn.execute(
                "SELECT id,email,role,is_approved FROM profiles WHERE email=? AND password=?",
                (body.get("email"), body.get("password")),
            ).fetchone()
            conn.close()
            if not prof:
                return json_response(self, 401, {"error": "Invalid credentials"})
            self.send_response(200)
            create_session(self, dict(prof))
            self.send_header("Content-Type", "application/json")
            redirect = "/admin" if prof["role"] == "admin" else ("/operator" if prof["role"] == "operator" else "/dashboard")
            raw = json.dumps({"redirect": redirect}).encode()
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if path == "/api/profile/setup":
            if not user or user["role"] != "student":
                return json_response(self, 403, {"error": "Forbidden"})
            conn = get_db()
            conn.execute(
                """
                INSERT INTO student_details (profile_id,full_name,dob,phone,address,education,photo_url,signature_url)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(profile_id) DO UPDATE SET
                full_name=excluded.full_name,dob=excluded.dob,phone=excluded.phone,address=excluded.address,
                education=excluded.education,photo_url=excluded.photo_url,signature_url=excluded.signature_url
                """,
                (
                    user["id"],
                    body.get("full_name"),
                    body.get("dob"),
                    body.get("phone"),
                    body.get("address"),
                    body.get("education"),
                    body.get("photo_url"),
                    body.get("signature_url"),
                ),
            )
            conn.execute("UPDATE profiles SET is_complete=1 WHERE id=?", (user["id"],))
            conn.commit()
            conn.close()
            return json_response(self, 200, {"is_complete": True, "redirect": "/dashboard"})

        if path == "/api/payments/cashfree/create":
            if not user or user["role"] != "student":
                return json_response(self, 403, {"error": "Forbidden"})
            exam_id = int(body.get("exam_id", 0))
            amount = float(body.get("amount", 49))
            try:
                data = create_cashfree_order(user["id"], exam_id, amount)
                return json_response(
                    self,
                    200,
                    {
                        "cashfree_order_id": data.get("order_id"),
                        "payment_session_id": data.get("payment_session_id"),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                return json_response(self, 400, {"error": f"cashfree_create_failed:{exc}"})

        if path == "/api/payments/cashfree/verify":
            if not user or user["role"] != "student":
                return json_response(self, 403, {"error": "Forbidden"})
            order_id = body.get("cashfree_order_id")
            if not order_id:
                return json_response(self, 400, {"error": "cashfree_order_id required"})
            try:
                payment = verify_cashfree_payment(order_id)
            except urllib.error.HTTPError as exc:
                return json_response(self, 400, {"error": f"cashfree_verify_failed:{exc.code}"})
            except Exception as exc:  # noqa: BLE001
                return json_response(self, 400, {"error": f"cashfree_verify_failed:{exc}"})
            if not payment:
                return json_response(self, 400, {"error": "payment_not_successful"})
            return json_response(self, 200, {"verified": True, "payment": payment})

        if path == "/api/orders/create":
            if not user or user["role"] != "student":
                return json_response(self, 403, {"error": "Forbidden"})
            exam_id = int(body.get("exam_id", 0))
            cf_order_id = body.get("cashfree_order_id")
            if not cf_order_id:
                return json_response(self, 400, {"error": "cashfree_order_id required"})

            conn = get_db()
            payment_row = conn.execute(
                "SELECT * FROM cashfree_payments WHERE cashfree_order_id=? AND student_id=? AND exam_id=?",
                (cf_order_id, user["id"], exam_id),
            ).fetchone()
            conn.close()
            if not payment_row:
                return json_response(self, 400, {"error": "payment_order_not_found"})
            try:
                payment = verify_cashfree_payment(cf_order_id)
            except Exception as exc:  # noqa: BLE001
                return json_response(self, 400, {"error": f"payment_verification_failed:{exc}"})
            if not payment:
                return json_response(self, 400, {"error": "Payment verification failed"})

            conn = get_db()
            created = now_iso()
            order_number = generate_order_number(conn)
            conn.execute(
                """
                INSERT INTO orders (order_number,student_id,exam_id,status,received_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (order_number, user["id"], exam_id, "RECEIVED", created, created, created),
            )
            oid = conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            conn.execute(
                "INSERT INTO order_status_log (order_id,status,changed_at) VALUES (?,?,?)",
                (oid, "RECEIVED", created),
            )
            conn.commit()
            conn.close()

            assigned_operator_id = assign_operator(oid)

            conn = get_db()
            exam = conn.execute("SELECT short_name,full_name FROM exams WHERE id=?", (exam_id,)).fetchone()
            student_email = conn.execute("SELECT email FROM profiles WHERE id=?", (user["id"],)).fetchone()["email"]
            if assigned_operator_id:
                student_msg = "Your form has been assigned to operator"
            else:
                student_msg = f"Your {exam['short_name']} application is received. Finding operator."
            conn.close()

            call_resend(student_email, f"Order {order_number} update", student_msg)

            return json_response(
                self,
                200,
                {
                    "order_id": oid,
                    "order_number": order_number,
                    "status": "ASSIGNED" if assigned_operator_id else "RECEIVED",
                    "redirect": f"/order/{oid}/confirm",
                },
            )

        m = re.match(r"^/operator/orders/(\d+)/submit$", path)
        if m:
            if not user or user["role"] != "operator":
                return json_response(self, 403, {"error": "Forbidden"})
            oid = int(m.group(1))
            app_no = body.get("application_number")
            proof_file_b64 = body.get("proof_file_b64")
            proof_url = body.get("proof_url")
            if not app_no:
                return json_response(self, 400, {"error": "application_number required"})
            if not proof_file_b64 and not proof_url:
                return json_response(self, 400, {"error": "proof required"})

            if proof_file_b64 and not proof_url:
                filename = f"proof_{oid}_{secrets.token_hex(4)}.png"
                path_on_disk = os.path.join(UPLOAD_DIR, filename)
                with open(path_on_disk, "wb") as fp:
                    fp.write(base64.b64decode(proof_file_b64.encode()))
                proof_url = f"/uploads/{filename}"

            conn = get_db()
            order = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if not order or order["operator_id"] != user["id"]:
                conn.close()
                return json_response(self, 403, {"error": "Forbidden"})

            ts = now_iso()
            update_order_status(
                conn,
                oid,
                "SUBMITTED",
                {"submitted_at": ts, "proof_url": proof_url, "application_number": app_no},
            )
            conn.execute(
                """
                INSERT INTO operator_stats (operator_id,accuracy_score,current_streak,total_completed,last_active_date)
                VALUES (?,95,1,1,?)
                ON CONFLICT(operator_id) DO UPDATE SET
                  current_streak=current_streak+1,
                  total_completed=total_completed+1,
                  last_active_date=excluded.last_active_date
                """,
                (user["id"], utc_today()),
            )
            row = conn.execute(
                "SELECT p.email as student_email, e.short_name FROM orders o JOIN profiles p ON p.id=o.student_id JOIN exams e ON e.id=o.exam_id WHERE o.id=?",
                (oid,),
            ).fetchone()
            conn.commit()
            conn.close()
            call_resend(
                row["student_email"],
                f"Your {row['short_name']} form is submitted!",
                f"Your form was submitted. Application number: {app_no}. Proof: {proof_url}",
            )
            return json_response(self, 200, {"status": "SUBMITTED", "proof_url": proof_url})

        m = re.match(r"^/admin/operators/(\d+)/(approve|suspend)$", path)
        if m:
            if not user or user["role"] != "admin":
                return json_response(self, 403, {"error": "Forbidden"})
            op_id = int(m.group(1))
            action = m.group(2)
            value = 1 if action == "approve" else 0
            conn = get_db()
            conn.execute("UPDATE profiles SET is_approved=? WHERE id=? AND role='operator'", (value, op_id))
            conn.commit()
            conn.close()
            return json_response(self, 200, {"ok": True})

        if path == "/admin/exams":
            if not user or user["role"] != "admin":
                return json_response(self, 403, {"error": "Forbidden"})
            conn = get_db()
            conn.execute(
                "INSERT INTO exams (short_name,full_name,official_url,is_active) VALUES (?,?,?,?)",
                (body["short_name"], body["full_name"], body["official_url"], int(body.get("is_active", 1))),
            )
            conn.commit()
            conn.close()
            return json_response(self, 200, {"ok": True})

        m = re.match(r"^/admin/exams/(\d+)/toggle$", path)
        if m:
            if not user or user["role"] != "admin":
                return json_response(self, 403, {"error": "Forbidden"})
            exam_id = int(m.group(1))
            conn = get_db()
            cur = conn.execute("SELECT is_active FROM exams WHERE id=?", (exam_id,)).fetchone()
            conn.execute("UPDATE exams SET is_active=? WHERE id=?", (0 if cur["is_active"] else 1, exam_id))
            conn.commit()
            conn.close()
            return json_response(self, 200, {"ok": True})

        m = re.match(r"^/admin/orders/(\d+)/reassign$", path)
        if m:
            if not user or user["role"] != "admin":
                return json_response(self, 403, {"error": "Forbidden"})
            oid = int(m.group(1))
            assigned = assign_operator(oid)
            return json_response(self, 200, {"ok": True, "assigned_operator_id": assigned})

        m = re.match(r"^/admin/orders/(\d+)/complete$", path)
        if m:
            if not user or user["role"] != "admin":
                return json_response(self, 403, {"error": "Forbidden"})
            oid = int(m.group(1))
            conn = get_db()
            update_order_status(conn, oid, "COMPLETED")
            conn.commit()
            conn.close()
            return json_response(self, 200, {"ok": True})

        return json_response(self, 404, {"error": "Not found"})


def run(port=8000):
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    run(int(os.environ.get("PORT", "8000")))
