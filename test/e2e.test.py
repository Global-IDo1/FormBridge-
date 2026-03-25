import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.cookiejar

BASE = "http://127.0.0.1:8123"
DB = "test_formbridge.db"
FAKE_CASHFREE = "http://127.0.0.1:9234/pg"


class CashfreeHandler(BaseHTTPRequestHandler):
    orders = {}

    def log_message(self, format, *args):
        return

    def do_POST(self):
        if self.path == "/pg/orders":
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            payload = json.loads(raw.decode())
            order_id = payload["order_id"]
            self.orders[order_id] = {"status": "SUCCESS", "cf_payment_id": f"pay_{order_id}"}
            body = json.dumps({"order_id": order_id, "payment_session_id": f"sess_{order_id}"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/pg/orders/") and self.path.endswith("/payments"):
            order_id = self.path.split("/")[3]
            item = self.orders.get(order_id)
            if not item:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps([{"payment_status": item["status"], "cf_payment_id": item["cf_payment_id"]}]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


if os.path.exists(DB):
    os.remove(DB)

fake_server = ThreadingHTTPServer(("127.0.0.1", 9234), CashfreeHandler)
thread = threading.Thread(target=fake_server.serve_forever, daemon=True)
thread.start()

env = os.environ.copy()
env["FORMBRIDGE_DB"] = DB
env["PORT"] = "8123"
env["CASHFREE_APP_ID"] = "test_app"
env["CASHFREE_SECRET_KEY"] = "test_secret"
env["CASHFREE_BASE_URL"] = FAKE_CASHFREE
env["RESEND_API_KEY"] = ""
proc = subprocess.Popen([sys.executable, "app.py"], env=env)
time.sleep(1)


def client():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    return opener


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def client_no_redirect():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect())
    return opener


def post(opener, path, data):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(req) as r:
        return r.status, json.loads(r.read().decode())


def get(opener, path):
    with opener.open(BASE + path) as r:
        raw = r.read().decode()
        ct = r.headers.get("Content-Type", "")
        if "application/json" in ct:
            return r.status, json.loads(raw)
        return r.status, raw


try:
    student = client()
    st, data = post(student, "/api/register", {"email": "student@test.com", "password": "pass", "role": "student"})
    assert st == 200 and data["redirect"] == "/profile/setup"

    st, login = post(student, "/api/login", {"email": "student@test.com", "password": "pass"})
    assert login["redirect"] == "/dashboard"

    st, _ = get(student, "/profile/setup")
    assert st == 200

    st, data = post(
        student,
        "/api/profile/setup",
        {
            "full_name": "Rohan Sharma",
            "dob": "2002-08-15",
            "phone": "9876543210",
            "address": "Lucknow",
            "education": "Bachelors",
            "photo_url": "photo.jpg",
            "signature_url": "sign.jpg",
        },
    )
    assert data["is_complete"] is True

    st, payment = post(student, "/api/payments/cashfree/create", {"exam_id": 1, "amount": 49})
    assert st == 200 and payment["cashfree_order_id"]
    cf_order = payment["cashfree_order_id"]

    st, verify = post(student, "/api/payments/cashfree/verify", {"cashfree_order_id": cf_order})
    assert st == 200 and verify["verified"] is True

    st, order = post(student, "/api/orders/create", {"exam_id": 1, "cashfree_order_id": cf_order})
    assert st == 200
    oid = order["order_id"]
    assert order["redirect"] == f"/order/{oid}/confirm"

    st, confirm_html = get(student, order["redirect"])
    assert "Order Confirmed" in confirm_html

    st, tr = get(client(), f"/api/track/{oid}")
    assert tr["status"] == "RECEIVED" and tr["status_text"] == "Finding operator"

    operator = client()
    st, data = post(operator, "/api/register", {"email": "operator@test.com", "password": "pass", "role": "operator"})
    assert st == 200 and data["message"] == "Account pending approval"

    st, op_login = post(operator, "/api/login", {"email": "operator@test.com", "password": "pass"})
    assert op_login["redirect"] == "/operator"

    st, text = get(operator, "/operator")
    assert "pending approval" in text

    admin = client()
    st, adm_login = post(admin, "/api/login", {"email": "admin@formbridge.in", "password": "adminpass"})
    assert adm_login["redirect"] == "/admin"

    st, ops = get(admin, "/admin/operators")
    op_id = [x["id"] for x in ops["operators"] if x["email"] == "operator@test.com"][0]
    st, _ = post(admin, f"/admin/operators/{op_id}/approve", {})
    assert st == 200
    st, admin_dash = get(admin, "/admin")
    assert "total_orders_today" in admin_dash

    st, payment2 = post(student, "/api/payments/cashfree/create", {"exam_id": 1, "amount": 49})
    post(student, "/api/payments/cashfree/verify", {"cashfree_order_id": payment2["cashfree_order_id"]})
    st, order2 = post(student, "/api/orders/create", {"exam_id": 1, "cashfree_order_id": payment2["cashfree_order_id"]})
    oid2 = order2["order_id"]

    st, tr2 = get(client(), f"/api/track/{oid2}")
    assert tr2["status"] == "ASSIGNED"
    assert any(row["status"] == "ASSIGNED" for row in tr2["timeline"])
    st, admin_orders = get(admin, "/admin/orders")
    assert any(o["id"] == oid2 for o in admin_orders["orders"])
    st, admin_exams = get(admin, "/admin/exams")
    assert len(admin_exams["exams"]) >= 1

    st, asg = get(operator, "/operator")
    assert asg["assignments"] and asg["assignments"][0]["id"] == oid2

    st, fill = get(operator, f"/operator/orders/{oid2}/fill")
    assert st == 200 and fill["status"] == "IN_PROGRESS"
    assert fill["full_name"].isupper() and "/" in fill["dob"]

    st, tr3 = get(client(), f"/api/track/{oid2}")
    assert tr3["status"] == "IN_PROGRESS"

    proof_b64 = "iVBORw0KGgo="
    st, submitted = post(
        operator,
        f"/operator/orders/{oid2}/submit",
        {"proof_file_b64": proof_b64, "application_number": "SSC1234"},
    )
    assert st == 200 and submitted["status"] == "SUBMITTED"

    st, tr4 = get(client(), f"/api/track/{oid2}")
    assert tr4["status"] == "SUBMITTED"
    assert tr4["order"]["application_number"] == "SSC1234"
    assert tr4["order"]["proof_url"].startswith("/uploads/")

    # non-assigned operator must be forbidden.
    other_op = client()
    post(other_op, "/api/register", {"email": "operator2@test.com", "password": "pass", "role": "operator"})
    st, ops2 = get(admin, "/admin/operators")
    op2_id = [x["id"] for x in ops2["operators"] if x["email"] == "operator2@test.com"][0]
    post(admin, f"/admin/operators/{op2_id}/approve", {})
    post(other_op, "/api/login", {"email": "operator2@test.com", "password": "pass"})
    try:
        get(other_op, f"/operator/orders/{oid2}/fill")
        raise AssertionError("other operator should not access")
    except urllib.error.HTTPError as exc:
        assert exc.code == 403

    # admin-only route protection
    student_no_redir = client_no_redirect()
    post(student_no_redir, "/api/login", {"email": "student@test.com", "password": "pass"})
    try:
        get(student_no_redir, "/admin")
        raise AssertionError("student must not access admin")
    except urllib.error.HTTPError as exc:
        assert exc.code == 302

    print("E2E workflow checks passed")
finally:
    proc.kill()
    proc.wait()
    fake_server.shutdown()

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM order_status_log")
    logs = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM email_log")
    emails = cur.fetchone()[0]
    cur.execute("SELECT current_streak,total_completed,last_active_date FROM operator_stats WHERE operator_id=(SELECT id FROM profiles WHERE email='operator@test.com')")
    stats = cur.fetchone()
    conn.close()
    assert logs >= 5
    assert emails >= 3
    assert stats[0] >= 1 and stats[1] >= 1 and stats[2]
