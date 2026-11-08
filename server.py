from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from collections.abc import Iterator


ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = Path(__file__).resolve().parent
DATABASE = Path(
    os.environ.get("DATABASE_PATH", str(BACKEND_DIR / "church.db"))
).resolve()
MAX_BODY_SIZE = 16_384
RATE_LIMIT_WINDOW = 15 * 60
RATE_LIMIT_COUNT = 5
MAX_DONATION_KES = 250_000
ALLOWED_SUBJECTS = {
    "General Inquiry",
    "Prayer Request",
    "First Time Guest",
    "Volunteering Opportunities",
}
EMAIL_PATTERN = re.compile(r"^[^@\s]{1,128}@[^@\s]{1,253}\.[^@\s]{2,63}$")
PHONE_PATTERN = re.compile(r"^254[17]\d{8}$")
RATE_LIMITS: dict[str, list[float]] = {}
RATE_LIMIT_LOCK = threading.Lock()
TOKEN_LOCK = threading.Lock()
TOKEN_CACHE: tuple[str, float] | None = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def database_connection() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database() -> None:
    with database_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                email TEXT NOT NULL,
                subject TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS donations (
                id TEXT PRIMARY KEY,
                amount_kes INTEGER NOT NULL,
                fund TEXT NOT NULL,
                phone TEXT NOT NULL,
                status TEXT NOT NULL,
                checkout_request_id TEXT UNIQUE,
                merchant_request_id TEXT,
                receipt_number TEXT,
                result_code INTEGER,
                result_description TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


def rate_limit_allows(key: str) -> bool:
    now = time.monotonic()
    with RATE_LIMIT_LOCK:
        attempts = [
            timestamp
            for timestamp in RATE_LIMITS.get(key, [])
            if now - timestamp < RATE_LIMIT_WINDOW
        ]
        if len(attempts) >= RATE_LIMIT_COUNT:
            RATE_LIMITS[key] = attempts
            return False
        attempts.append(now)
        RATE_LIMITS[key] = attempts
        return True


def configured_payment_settings() -> tuple[str, str, str, str, str]:
    environment = os.environ.get("M_PESA_ENV", "sandbox").lower()
    if environment not in {"sandbox", "production"}:
        raise ValueError("M_PESA_ENV must be sandbox or production.")
    consumer_key = os.environ.get("M_PESA_CONSUMER_KEY", "").strip()
    consumer_secret = os.environ.get("M_PESA_CONSUMER_SECRET", "").strip()
    shortcode = os.environ.get("M_PESA_SHORTCODE", "").strip()
    passkey = os.environ.get("M_PESA_PASSKEY", "").strip()
    callback_url = os.environ.get("M_PESA_CALLBACK_URL", "").strip()
    if not all((consumer_key, consumer_secret, shortcode, passkey, callback_url)):
        raise ValueError("M-Pesa is not configured.")
    parsed_callback = urlparse(callback_url)
    if parsed_callback.scheme != "https" or not parsed_callback.netloc:
        raise ValueError("M_PESA_CALLBACK_URL must be a public HTTPS URL.")
    return environment, consumer_key, consumer_secret, shortcode, passkey


def mpesa_base_url(environment: str) -> str:
    if environment == "production":
        return "https://api.safaricom.co.ke"
    return "https://sandbox.safaricom.co.ke"


def mpesa_access_token() -> tuple[str, str, str, str]:
    global TOKEN_CACHE
    environment, consumer_key, consumer_secret, shortcode, passkey = (
        configured_payment_settings()
    )
    with TOKEN_LOCK:
        if TOKEN_CACHE and TOKEN_CACHE[1] > time.monotonic():
            return TOKEN_CACHE[0], environment, shortcode, passkey
        credentials = base64.b64encode(
            f"{consumer_key}:{consumer_secret}".encode("utf-8")
        ).decode("ascii")
        request = Request(
            f"{mpesa_base_url(environment)}/oauth/v1/generate?grant_type=client_credentials",
            headers={"Authorization": f"Basic {credentials}"},
        )
        try:
            with urlopen(request, timeout=20) as response:
                token_response = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            raise RuntimeError("Could not authenticate with M-Pesa.") from error
        if not isinstance(token_response, dict):
            raise RuntimeError("M-Pesa returned an unexpected authentication response.")
        access_token = token_response.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("M-Pesa did not return an access token.")
        try:
            expires_in = int(token_response.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600
        TOKEN_CACHE = (access_token, time.monotonic() + max(0, expires_in - 60))
        return access_token, environment, shortcode, passkey


def mpesa_timestamp() -> str:
    nairobi_time = datetime.now(timezone.utc) + timedelta(hours=3)
    return nairobi_time.strftime("%Y%m%d%H%M%S")


def mpesa_password(shortcode: str, passkey: str, timestamp: str) -> str:
    return base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode("utf-8")).decode(
        "ascii"
    )


def start_stk_push(
    amount: int, phone: str, donation_id: str, fund: str
) -> dict[str, object]:
    token, environment, shortcode, passkey = mpesa_access_token()
    timestamp = mpesa_timestamp()
    payload: dict[str, object] = {
        "BusinessShortCode": shortcode,
        "Password": mpesa_password(shortcode, passkey, timestamp),
        "Timestamp": timestamp,
        "TransactionType": os.environ.get(
            "M_PESA_TRANSACTION_TYPE", "CustomerPayBillOnline"
        ),
        "Amount": amount,
        "PartyA": phone,
        "PartyB": shortcode,
        "PhoneNumber": phone,
        "CallBackURL": os.environ["M_PESA_CALLBACK_URL"].strip(),
        "AccountReference": f"CHURCH-{donation_id[:12]}",
        "TransactionDesc": fund[:20],
    }
    request = Request(
        f"{mpesa_base_url(environment)}/mpesa/stkpush/v1/processrequest",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=25) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        raise RuntimeError("Could not start the M-Pesa payment.") from error
    if not isinstance(result, dict) or str(result.get("ResponseCode")) != "0":
        raise RuntimeError("M-Pesa could not start the payment.")
    if not result.get("CheckoutRequestID"):
        raise RuntimeError("M-Pesa returned no checkout request ID.")
    return result


def query_stk_status(checkout_request_id: str) -> dict[str, object]:
    token, environment, shortcode, passkey = mpesa_access_token()
    timestamp = mpesa_timestamp()
    payload: dict[str, object] = {
        "BusinessShortCode": shortcode,
        "Password": mpesa_password(shortcode, passkey, timestamp),
        "Timestamp": timestamp,
        "CheckoutRequestID": checkout_request_id,
    }
    request = Request(
        f"{mpesa_base_url(environment)}/mpesa/stkpushquery/v1/query",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=25) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        raise RuntimeError("Could not verify the M-Pesa payment.") from error
    if not isinstance(result, dict):
        raise RuntimeError("M-Pesa returned an unexpected query response.")
    return result


def normalize_phone(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    phone = re.sub(r"[\s()+-]", "", value)
    if phone.startswith("0"):
        phone = f"254{phone[1:]}"
    if not PHONE_PATTERN.fullmatch(phone):
        return None
    return phone


class ChurchRequestHandler(BaseHTTPRequestHandler):
    server_version = "ChurchBackend/1.0"

    def log_message(self, format: str, *args: object) -> None:
        logging.info("%s - %s", self.address_string(), format % args)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_json(500, {"error": "The requested page is unavailable."})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, object] | None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            self.send_json(415, {"error": "Send the request as JSON."})
            return None
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Invalid request."})
            return None
        if content_length <= 0 or content_length > MAX_BODY_SIZE:
            self.send_json(413, {"error": "Request is empty or too large."})
            return None
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {"error": "Invalid JSON request."})
            return None
        if not isinstance(payload, dict):
            self.send_json(400, {"error": "Invalid request."})
            return None
        return payload

    def admin_authorized(self) -> bool:
        configured_token = os.environ.get("ADMIN_API_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        if not configured_token or not supplied.startswith("Bearer "):
            return False
        return hmac.compare_digest(supplied[7:], configured_token)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.send_file(
                ROOT / "Kajiado_Imarika_Deliverence_Church_website.html",
                "text/html; charset=utf-8",
            )
            return
        if path == "/admin":
            self.send_file(BACKEND_DIR / "admin.html", "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self.send_json(200, {"status": "ok"})
            return
        if path in {"/api/admin/messages", "/api/admin/donations"}:
            if not self.admin_authorized():
                self.send_json(401, {"error": "Admin authorization required."})
                return
            table = "messages" if path.endswith("/messages") else "donations"
            with database_connection() as connection:
                rows = connection.execute(
                    f"SELECT * FROM {table} ORDER BY created_at DESC LIMIT 500"
                ).fetchall()
            self.send_json(200, {"items": [dict(row) for row in rows]})
            return
        self.send_json(404, {"error": "Not found."})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/mpesa/callback":
            self.handle_mpesa_callback()
            return
        if path not in {"/api/contact", "/api/donations"}:
            self.send_json(404, {"error": "Not found."})
            return
        remote_ip = self.client_address[0]
        if not rate_limit_allows(f"{path}:{remote_ip}"):
            self.send_json(429, {"error": "Too many attempts. Please try again later."})
            return
        payload = self.read_json()
        if payload is None:
            return
        if path == "/api/contact":
            self.handle_contact(payload)
        else:
            self.handle_donation(payload)

    def handle_contact(self, payload: dict[str, object]) -> None:
        first_name = self.clean_text(payload.get("firstName"), 80)
        last_name = self.clean_text(payload.get("lastName"), 80)
        email = self.clean_text(payload.get("email"), 254).lower()
        subject = self.clean_text(payload.get("subject"), 40)
        message = self.clean_text(payload.get("message"), 5000)
        if not first_name or not last_name or not EMAIL_PATTERN.fullmatch(email):
            self.send_json(400, {"error": "Enter a valid name and email address."})
            return
        if subject not in ALLOWED_SUBJECTS or not message:
            self.send_json(400, {"error": "Choose a subject and enter a message."})
            return
        with database_connection() as connection:
            connection.execute(
                """
                INSERT INTO messages (first_name, last_name, email, subject, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (first_name, last_name, email, subject, message, utc_now()),
            )
        self.send_json(201, {"message": "Your message has been received."})

    def handle_donation(self, payload: dict[str, object]) -> None:
        try:
            configured_payment_settings()
        except ValueError as error:
            message = str(error)
            status = 503 if message == "M-Pesa is not configured." else 500
            self.send_json(status, {"error": message})
            return

        amount_value = payload.get("amount")
        if isinstance(amount_value, int) and not isinstance(amount_value, bool):
            amount = amount_value
        elif isinstance(amount_value, str) and re.fullmatch(r"\d+", amount_value):
            amount = int(amount_value)
        else:
            amount = 0
        fund = self.clean_text(payload.get("fund"), 80)
        phone = normalize_phone(payload.get("phone"))
        allowed_funds = {
            "General Tithes & Offerings",
            "Building & Renovation Fund",
            "Missions & Outreach",
        }
        if not 1 <= amount <= MAX_DONATION_KES:
            self.send_json(
                400,
                {"error": f"Enter an amount between KES 1 and KES {MAX_DONATION_KES:,}."},
            )
            return
        if not phone:
            self.send_json(400, {"error": "Enter a valid Kenyan M-Pesa phone number."})
            return
        if fund not in allowed_funds:
            self.send_json(400, {"error": "Choose a valid giving fund."})
            return

        donation_id = uuid.uuid4().hex
        now = utc_now()
        with database_connection() as connection:
            connection.execute(
                """
                INSERT INTO donations
                    (id, amount_kes, fund, phone, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'initiating', ?, ?)
                """,
                (donation_id, amount, fund, phone, now, now),
            )
        try:
            result = start_stk_push(amount, phone, donation_id, fund)
        except RuntimeError as error:
            logging.warning("M-Pesa STK request failed: %s", error)
            with database_connection() as connection:
                connection.execute(
                    "UPDATE donations SET status = 'failed', updated_at = ? WHERE id = ?",
                    (utc_now(), donation_id),
                )
            self.send_json(
                502,
                {"error": "Could not start the M-Pesa request. Please try again later."},
            )
            return
        with database_connection() as connection:
            connection.execute(
                """
                UPDATE donations
                SET status = 'stk_sent', checkout_request_id = ?,
                    merchant_request_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(result["CheckoutRequestID"]),
                    str(result.get("MerchantRequestID", "")),
                    utc_now(),
                    donation_id,
                ),
            )
        self.send_json(
            202,
            {
                "message": "Check your phone and enter your M-Pesa PIN to complete the gift.",
                "donationId": donation_id,
            },
        )

    def handle_mpesa_callback(self) -> None:
        payload = self.read_json()
        if payload is None:
            return
        body = payload.get("Body")
        callback = body.get("stkCallback") if isinstance(body, dict) else None
        if not isinstance(callback, dict):
            self.send_json(400, {"error": "Invalid M-Pesa callback."})
            return
        checkout_id = callback.get("CheckoutRequestID")
        if not isinstance(checkout_id, str) or not checkout_id:
            self.send_json(400, {"error": "Invalid M-Pesa callback."})
            return
        try:
            result_code = int(callback.get("ResultCode"))
        except (TypeError, ValueError):
            self.send_json(400, {"error": "Invalid M-Pesa callback."})
            return
        description = self.clean_text(callback.get("ResultDesc"), 255)
        receipt_number = None
        metadata = callback.get("CallbackMetadata")
        items = metadata.get("Item") if isinstance(metadata, dict) else None
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("Name") == "MpesaReceiptNumber":
                    receipt_number = self.clean_text(item.get("Value"), 32)
                    break

        with database_connection() as connection:
            donation = connection.execute(
                "SELECT id, status FROM donations WHERE checkout_request_id = ?",
                (checkout_id,),
            ).fetchone()
            if donation is None:
                self.send_json(200, {"ResultCode": 0, "ResultDesc": "Accepted"})
                return
            if donation["status"] == "paid":
                self.send_json(200, {"ResultCode": 0, "ResultDesc": "Accepted"})
                return
            connection.execute(
                """
                UPDATE donations
                SET status = ?, result_code = ?, result_description = ?,
                    receipt_number = COALESCE(?, receipt_number), updated_at = ?
                WHERE checkout_request_id = ?
                """,
                (
                    "callback_received" if result_code == 0 else "failed",
                    result_code,
                    description,
                    receipt_number,
                    utc_now(),
                    checkout_id,
                ),
            )

        if result_code == 0:
            try:
                query_result = query_stk_status(checkout_id)
                query_code = int(query_result.get("ResultCode"))
                verified_status = "paid" if query_code == 0 else "failed"
                with database_connection() as connection:
                    connection.execute(
                        """
                        UPDATE donations
                        SET status = ?, result_code = ?, result_description = ?, updated_at = ?
                        WHERE checkout_request_id = ?
                        """,
                        (
                            verified_status,
                            query_code,
                            self.clean_text(query_result.get("ResultDesc"), 255),
                            utc_now(),
                            checkout_id,
                        ),
                    )
            except (RuntimeError, TypeError, ValueError) as error:
                logging.warning("Could not verify M-Pesa callback: %s", error)
        self.send_json(200, {"ResultCode": 0, "ResultDesc": "Accepted"})

    @staticmethod
    def clean_text(value: object, max_length: int) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip()[:max_length]


def main() -> None:
    admin_token = os.environ.get("ADMIN_API_TOKEN", "")
    if len(admin_token) < 32:
        raise SystemExit(
            "Set ADMIN_API_TOKEN to a random secret of at least 32 characters."
        )
    initialize_database()
    host = os.environ.get("HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("PORT", "8000"))
    except ValueError as error:
        raise SystemExit("PORT must be a number.") from error
    server = ThreadingHTTPServer((host, port), ChurchRequestHandler)
    logging.info("Church website and API listening on http://%s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Stopping the church backend.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
