import json
import os
import random
import re
import hashlib
import hmac
from datetime import datetime
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from flask import Flask, render_template, request, jsonify
from google import genai
from google.genai import types

app = Flask(__name__)


client = genai.Client(api_key="AIzaSyD9usgM0WdJ0nfsMz7iSpAaSAwR8-grZGE")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")



def _as_bool(value: str) -> bool:
    return str(value or "").strip().lower() == "yes"


def _supabase_ready() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def _supabase_request(method: str, path_with_query: str, payload=None, prefer_header: str | None = None):
    if not _supabase_ready():
        raise RuntimeError("Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.")

    url = f"{SUPABASE_URL}/rest/v1/{path_with_query}"

    body = None
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer_header or "return=representation",
    }


    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    req = urlrequest.Request(url=url, data=body, headers=headers, method=method.upper())
    try:
        with urlrequest.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8") if resp else ""
            return json.loads(raw) if raw else []
    except urlerror.HTTPError as http_err:
        err_body = http_err.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Supabase HTTP {http_err.code}: {err_body}") from http_err


def _supabase_get_first(table: str, filters: dict) -> dict | None:
    if not filters:
        return None
    query_parts = ["select=*"]
    for key, value in filters.items():
        if value is None or str(value).strip() == "":
            continue
        encoded = urlparse.quote(str(value), safe="")
        query_parts.append(f"{key}=eq.{encoded}")
    query_parts.append("limit=1")
    query = "&".join(query_parts)
    rows = _supabase_request("GET", f"{table}?{query}")
    return rows[0] if rows else None


def _supabase_patch_by_id(table: str, row_id: str, payload: dict):
    encoded_id = urlparse.quote(str(row_id), safe="")
    return _supabase_request("PATCH", f"{table}?id=eq.{encoded_id}", payload)


def _supabase_upsert_by_user_id(table: str, payload: dict):
    return _supabase_request(
        "POST",
        f"{table}?on_conflict=user_id",
        payload,
        prefer_header="resolution=merge-duplicates,return=representation",
    )


def _random_digits(length: int) -> str:
    return "".join(random.choice("0123456789") for _ in range(length))


def _generate_unique_account_number() -> str:
    for _ in range(20):
        candidate = f"02{_random_digits(12)}"
        if not _supabase_get_first("bank_accounts", {"account_number": candidate}):
            return candidate
    return f"02{datetime.utcnow().strftime('%y%m%d%H%M%S')}"[:14]


def _generate_unique_customer_id() -> str:
    for _ in range(20):
        candidate = f"CUST{_random_digits(8)}"
        if not _supabase_get_first("bank_accounts", {"customer_id": candidate}):
            return candidate
    return f"CUST{datetime.utcnow().strftime('%H%M%S%f')[:8]}"


def _find_or_create_user_id(basic_details: dict, contact_details: dict) -> str:
    mobile = (contact_details.get("mobile") or "").strip()
    email = (contact_details.get("email") or "").strip().lower()
    existing = None
    if mobile:
        existing = _supabase_get_first("users", {"mobile": mobile})
    if not existing and email:
        existing = _supabase_get_first("users", {"email": email})

    user_payload = {
        "full_name": basic_details.get("fullName"),
        "dob": _normalize_date(basic_details.get("dob", "")) or None,
        "gender": basic_details.get("gender"),
        "parent_name": basic_details.get("parentName"),
        "marital_status": basic_details.get("maritalStatus"),
        "mobile": mobile or None,
        "email": email or None,
    }

    if existing and existing.get("id"):
        _supabase_patch_by_id("users", existing["id"], user_payload)
        return existing["id"]

    inserted = _supabase_request("POST", "users", user_payload)
    if not inserted or not inserted[0].get("id"):
        raise RuntimeError("Could not create user in Supabase.")
    return inserted[0]["id"]


def get_predefined_reply(message: str):
    text = (message or "").strip().lower()

    if any(k in text for k in ["what is login", "login here", "how to login", "what does login do"]):
        return (
            "Login is for existing customers who already have an account. "
            "Use Login to access your dashboard and manage your account. "
            "If you are new, use Get Started to open a new account."
        )

    if any(k in text for k in ["what is get started", "get started", "how do i start", "open account"]):
        return (
            "Get Started begins the new account opening process. "
            "I will ask a few simple questions, collect your details, "
            "and guide you step by step."
        )

    return None


def get_dashboard_predefined_reply(message: str):
    text = (message or "").strip().lower()

    if any(k in text for k in ["send money", "transfer money", "make transfer", "pay someone"]):
        return (
            "Sure. Steps to send money:\n"
            "1. Open Quick Actions and tap Send.\n"
            "2. Select saved beneficiary or add a new one.\n"
            "3. Enter amount and optional note.\n"
            "4. Review account and recipient details.\n"
            "5. Confirm with your app PIN/OTP.\n"
            "6. Check Recent Transactions for the success entry."
        )

    if any(k in text for k in ["monthly insights", "spending insights", "insights", "monthly summary"]):
        return (
            "To view monthly insights:\n"
            "1. Go to the Insights card on the dashboard.\n"
            "2. Review category-wise spend highlights.\n"
            "3. Compare this month against last month.\n"
            "4. Set a budget alert for categories where spending is high."
        )

    if any(k in text for k in ["account balance", "check balance", "my balance"]):
        return (
            "To check balance quickly:\n"
            "1. Open dashboard Home.\n"
            "2. See Account Balance card at the top.\n"
            "3. Tap View Statement for detailed credits/debits."
        )

    if any(k in text for k in ["pay bill", "electricity bill", "bill payment"]):
        return (
            "Steps to pay a bill:\n"
            "1. Tap Pay Bills in Quick Actions.\n"
            "2. Choose bill type and provider.\n"
            "3. Enter customer/account number.\n"
            "4. Verify bill amount.\n"
            "5. Confirm payment and save receipt."
        )

    if any(k in text for k in ["scan and pay", "scan pay", "qr pay"]):
        return (
            "Steps for Scan and Pay:\n"
            "1. Tap Scan and Pay.\n"
            "2. Allow camera permission.\n"
            "3. Scan merchant QR code.\n"
            "4. Enter amount if needed.\n"
            "5. Confirm payment with PIN."
        )

    if any(k in text for k in ["request money", "collect money"]):
        return (
            "Steps to request money:\n"
            "1. Tap Request in Quick Actions.\n"
            "2. Select contact.\n"
            "3. Enter amount and reason.\n"
            "4. Send request.\n"
            "5. Track status in transactions."
        )

    return None


def _normalize_text(value: str) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _normalize_name(value: str) -> str:
    return _normalize_text(value)


def _normalize_digits(value: str) -> str:
    if not value:
        return ""
    return re.sub(r"\D", "", value)


def _normalize_date(value: str) -> str:
    if not value:
        return ""
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return value


def _hash_pin(pin: str) -> str:
    return hashlib.sha256((pin or "").encode("utf-8")).hexdigest()


def _save_login_credentials(user_id: str, mobile: str, pin: str) -> str:
    pin_hash = _hash_pin(pin)
    payload = {
        "user_id": user_id,
        "mobile": mobile,
        "pin_hash": pin_hash,
        "updated_at": datetime.utcnow().isoformat(),
    }

    # Preferred dedicated auth table with unique user_id.
    try:
        _supabase_upsert_by_user_id("user_auth", payload)
        return "user_auth"
    except Exception:
        pass

    # Fallback for schemas that store auth fields directly in users.
    try:
        _supabase_patch_by_id("users", user_id, {"mobile": mobile, "app_pin_hash": pin_hash})
        return "users"
    except Exception:
        pass

    # Guaranteed-table fallback: store auth metadata in document_verifications.extracted_data
    try:
        doc_row = _supabase_get_first("document_verifications", {"user_id": user_id}) or {}
        extracted_data = doc_row.get("extracted_data") if isinstance(doc_row.get("extracted_data"), dict) else {}
        extracted_data["auth_mobile"] = mobile
        extracted_data["pin_hash"] = pin_hash
        payload = {
            "user_id": user_id,
            "status": doc_row.get("status") or "pending",
            "warnings": doc_row.get("warnings") if isinstance(doc_row.get("warnings"), list) else [],
            "extracted_data": extracted_data,
        }
        _supabase_upsert_by_user_id("document_verifications", payload)
        return "document_verifications"
    except Exception as exc:
        raise RuntimeError(
            "Could not save login credentials in database. Ensure Supabase is configured and accessible."
        ) from exc


def _find_user_by_mobile_and_pin(mobile: str, pin: str) -> dict | None:
    pin_hash = _hash_pin(pin)

    # Primary lookup from dedicated auth table.
    try:
        auth_row = _supabase_get_first("user_auth", {"mobile": mobile})
        if auth_row and auth_row.get("pin_hash") and hmac.compare_digest(str(auth_row["pin_hash"]), pin_hash):
            user_id = auth_row.get("user_id")
            if user_id:
                return _supabase_get_first("users", {"id": user_id})
    except Exception:
        pass

    # Fallback lookup from users table.
    try:
        user_row = _supabase_get_first("users", {"mobile": mobile})
        if not user_row:
            return None
        stored = str(user_row.get("app_pin_hash") or "")
        if stored and hmac.compare_digest(stored, pin_hash):
            return user_row
    except Exception:
        pass

    # Fallback lookup from document_verifications.extracted_data
    try:
        user_row = _supabase_get_first("users", {"mobile": mobile})
        if not user_row or not user_row.get("id"):
            return None
        doc_row = _supabase_get_first("document_verifications", {"user_id": user_row.get("id")}) or {}
        extracted_data = doc_row.get("extracted_data") if isinstance(doc_row.get("extracted_data"), dict) else {}
        stored_mobile = _normalize_digits(extracted_data.get("auth_mobile", ""))
        stored_hash = str(extracted_data.get("pin_hash") or "")
        if stored_mobile == mobile and stored_hash and hmac.compare_digest(stored_hash, pin_hash):
            return user_row
    except Exception:
        pass

    return None


def _verify_pin_for_user(user_id: str, pin: str) -> bool:
    pin_hash = _hash_pin(pin)

    try:
        auth_row = _supabase_get_first("user_auth", {"user_id": user_id})
        stored = str(auth_row.get("pin_hash") or "") if auth_row else ""
        if stored and hmac.compare_digest(stored, pin_hash):
            return True
    except Exception:
        pass

    try:
        user_row = _supabase_get_first("users", {"id": user_id})
        stored = str(user_row.get("app_pin_hash") or "") if user_row else ""
        if stored and hmac.compare_digest(stored, pin_hash):
            return True
    except Exception:
        pass

    try:
        doc_row = _supabase_get_first("document_verifications", {"user_id": user_id}) or {}
        extracted_data = doc_row.get("extracted_data") if isinstance(doc_row.get("extracted_data"), dict) else {}
        stored = str(extracted_data.get("pin_hash") or "")
        if stored and hmac.compare_digest(stored, pin_hash):
            return True
    except Exception:
        pass

    return False


def _format_txn_datetime(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y, %I:%M %p")
    except Exception:
        return text


def _normalize_txn_row(row: dict) -> dict:
    amount_raw = row.get("amount")
    try:
        amount_val = float(amount_raw)
    except Exception:
        amount_val = 0.0

    beneficiary = (
        row.get("beneficiary_name")
        or row.get("payee_name")
        or row.get("contact_name")
        or row.get("description")
        or "Transfer"
    )
    reference = row.get("txn_reference") or row.get("reference") or row.get("id") or "-"
    created_at = row.get("created_at") or row.get("transaction_date") or ""
    status = row.get("status") or "success"
    txn_type = row.get("txn_type") or "debit"
    sign = "+" if str(txn_type).lower() == "credit" else "-"

    return {
        "beneficiary_name": str(beneficiary),
        "amount": amount_val,
        "amount_display": f"Rs {amount_val:,.2f}",
        "amount_signed_display": f"{sign} Rs {amount_val:,.2f}",
        "reference": str(reference),
        "created_at": str(created_at),
        "created_at_display": _format_txn_datetime(created_at),
        "status": str(status),
        "txn_type": str(txn_type),
    }


def _store_payment_transaction(
    user_id: str,
    contact: str,
    amount: float,
    reference: str,
    txn_type: str = "debit",
    description: str | None = None,
    status: str = "success",
) -> str:
    desc_text = description or (f"Transfer to {contact}" if txn_type == "debit" else f"Received from {contact}")
    record = {
        "user_id": user_id,
        "beneficiary_name": contact,
        "amount": amount,
        "txn_type": txn_type,
        "status": status,
        "description": desc_text,
        "txn_reference": reference,
        "created_at": datetime.utcnow().isoformat(),
    }

    for table_name in ("transactions", "payment_transactions", "bank_transactions"):
        try:
            _supabase_request("POST", table_name, record)
            return table_name
        except Exception:
            continue

    try:
        doc_row = _supabase_get_first("document_verifications", {"user_id": user_id}) or {}
        extracted_data = doc_row.get("extracted_data") if isinstance(doc_row.get("extracted_data"), dict) else {}
        history = extracted_data.get("payments_history") if isinstance(extracted_data.get("payments_history"), list) else []
        history.insert(0, record)
        extracted_data["payments_history"] = history[:500]
        payload = {
            "user_id": user_id,
            "status": doc_row.get("status") or "pending",
            "warnings": doc_row.get("warnings") if isinstance(doc_row.get("warnings"), list) else [],
            "extracted_data": extracted_data,
        }
        _supabase_upsert_by_user_id("document_verifications", payload)
        return "document_verifications"
    except Exception:
        return ""


def _get_user_transactions(user_id: str, limit: int = 25) -> list[dict]:
    encoded_user_id = urlparse.quote(str(user_id), safe="")
    for table_name in ("transactions", "payment_transactions", "bank_transactions"):
        try:
            rows = _supabase_request(
                "GET",
                f"{table_name}?user_id=eq.{encoded_user_id}&order=created_at.desc&limit={int(limit)}",
            )
            if isinstance(rows, list) and rows:
                return [_normalize_txn_row(r if isinstance(r, dict) else {}) for r in rows]
        except Exception:
            continue

    try:
        doc_row = _supabase_get_first("document_verifications", {"user_id": user_id}) or {}
        extracted_data = doc_row.get("extracted_data") if isinstance(doc_row.get("extracted_data"), dict) else {}
        history = extracted_data.get("payments_history") if isinstance(extracted_data.get("payments_history"), list) else []
        normalized = [_normalize_txn_row(r if isinstance(r, dict) else {}) for r in history[:limit]]
        normalized.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return normalized
    except Exception:
        return []


def _extract_document_fields(file_bytes: bytes, mime_type: str, label: str) -> dict:
    prompt = f"""
You are an OCR extraction assistant for bank KYC.
Extract visible user identity and address fields from this {label}.
Return strict JSON only, no markdown, using keys:
{{
  "document_type": "",
  "full_name": "",
  "dob": "",
  "aadhaar_number": "",
  "pan_number": "",
  "address_line": "",
  "city": "",
  "state": "",
  "pincode": "",
  "country": ""
}}
If a field is not visible, return empty string.
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Part.from_text(text=prompt),
            types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
        ],
    )

    raw_text = (response.text or "").strip()
    cleaned = raw_text
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json", "", 1).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        parsed = {
            "document_type": label,
            "full_name": "",
            "dob": "",
            "aadhaar_number": "",
            "pan_number": "",
            "address_line": "",
            "city": "",
            "state": "",
            "pincode": "",
            "country": "",
        }
    return parsed


def _extract_document_fields_batch(docs: list[tuple[str, bytes, str]]) -> list[dict]:
    prompt = """
You are an OCR extraction assistant for bank KYC.
You will receive multiple documents in order.
Extract visible user identity/address fields for each document.
Return strict JSON only (no markdown) in this exact format:
{
  "documents": [
    {
      "source_label": "",
      "document_type": "",
      "full_name": "",
      "dob": "",
      "aadhaar_number": "",
      "pan_number": "",
      "address_line": "",
      "city": "",
      "state": "",
      "pincode": "",
      "country": ""
    }
  ]
}
If a field is not visible, keep it as empty string.
"""
    contents = [types.Part.from_text(text=prompt)]
    for idx, (label, file_bytes, mime_type) in enumerate(docs, start=1):
        contents.append(types.Part.from_text(text=f"Document {idx}: {label}"))
        contents.append(types.Part.from_bytes(data=file_bytes, mime_type=mime_type))

    response = client.models.generate_content(model="gemini-2.5-flash", contents=contents)
    raw_text = (response.text or "").strip()
    cleaned = raw_text
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json", "", 1).strip()

    parsed_docs = []
    try:
        parsed = json.loads(cleaned)
        parsed_docs = parsed.get("documents", []) if isinstance(parsed, dict) else []
    except json.JSONDecodeError:
        parsed_docs = []

    if not isinstance(parsed_docs, list):
        parsed_docs = []

    normalized = []
    for idx, (label, _bytes, _mime) in enumerate(docs):
        doc = parsed_docs[idx] if idx < len(parsed_docs) and isinstance(parsed_docs[idx], dict) else {}
        normalized.append(
            {
                "source_label": doc.get("source_label") or label,
                "document_type": doc.get("document_type", ""),
                "full_name": doc.get("full_name", ""),
                "dob": doc.get("dob", ""),
                "aadhaar_number": doc.get("aadhaar_number", ""),
                "pan_number": doc.get("pan_number", ""),
                "address_line": doc.get("address_line", ""),
                "city": doc.get("city", ""),
                "state": doc.get("state", ""),
                "pincode": doc.get("pincode", ""),
                "country": doc.get("country", ""),
            }
        )
    return normalized


def _quota_wait_seconds(error_text: str) -> float | None:
    match = re.search(r"retry in ([0-9.]+)s", error_text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _compare_user_vs_extracted(user_details: dict, extracted_docs: list[dict]) -> list[str]:
    warnings = []

    basic = user_details.get("basicDetails", {}) or {}
    contact = user_details.get("contactDetails", {}) or {}
    identity = user_details.get("identityDetails", {}) or {}

    user_full_name = _normalize_name(basic.get("fullName", ""))
    user_dob = _normalize_date(basic.get("dob", ""))
    user_aadhaar = _normalize_digits(identity.get("aadhaar", ""))
    user_pan = _normalize_text(identity.get("pan", ""))
    user_city = _normalize_name(contact.get("city", ""))
    user_state = _normalize_name(contact.get("state", ""))
    user_pincode = _normalize_digits(contact.get("pincode", ""))
    user_country = _normalize_name(contact.get("country", ""))

    extracted_full_name = ""
    extracted_dob = ""
    extracted_aadhaar = ""
    extracted_pan = ""
    extracted_city = ""
    extracted_state = ""
    extracted_pincode = ""
    extracted_country = ""

    for doc in extracted_docs:
        extracted_full_name = extracted_full_name or _normalize_name(doc.get("full_name", ""))
        extracted_dob = extracted_dob or _normalize_date(doc.get("dob", ""))
        extracted_aadhaar = extracted_aadhaar or _normalize_digits(doc.get("aadhaar_number", ""))
        extracted_pan = extracted_pan or _normalize_text(doc.get("pan_number", ""))
        extracted_city = extracted_city or _normalize_name(doc.get("city", ""))
        extracted_state = extracted_state or _normalize_name(doc.get("state", ""))
        extracted_pincode = extracted_pincode or _normalize_digits(doc.get("pincode", ""))
        extracted_country = extracted_country or _normalize_name(doc.get("country", ""))

    if user_full_name and not extracted_full_name:
        warnings.append("Could not clearly extract full name from uploaded documents.")
    if user_dob and not extracted_dob:
        warnings.append("Could not clearly extract date of birth from uploaded documents.")
    if user_aadhaar and not extracted_aadhaar:
        warnings.append("Could not clearly extract Aadhaar number from uploaded documents.")
    if user_pan and not extracted_pan:
        warnings.append("Could not clearly extract PAN number from uploaded documents.")

    if user_full_name and extracted_full_name and user_full_name != extracted_full_name:
        warnings.append("Name in documents does not match the entered full name.")
    if user_dob and extracted_dob and user_dob != extracted_dob:
        warnings.append("Date of birth in documents does not match the entered DOB.")
    if user_aadhaar and extracted_aadhaar and user_aadhaar != extracted_aadhaar:
        warnings.append("Aadhaar number in document does not match the entered Aadhaar number.")
    if user_pan and extracted_pan and user_pan != extracted_pan:
        warnings.append("PAN number in document does not match the entered PAN number.")
    if user_city and extracted_city and user_city != extracted_city:
        warnings.append("City in address proof does not match entered city.")
    if user_state and extracted_state and user_state != extracted_state:
        warnings.append("State in address proof does not match entered state.")
    if user_pincode and extracted_pincode and user_pincode != extracted_pincode:
        warnings.append("Pincode in address proof does not match entered pincode.")
    if user_country and extracted_country and user_country != extracted_country:
        warnings.append("Country in address proof does not match entered country.")

    return warnings


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/get-started")
def get_started():
    return render_template("get_started.html")


@app.route("/dashboard")
def dashboard():
    user_id = (request.args.get("user_id") or "").strip()
    user_name = "Shravani"
    account_number = ""
    customer_id = ""
    ifsc_code = ""
    branch_name = ""

    if user_id and _supabase_ready():
        try:
            user_row = _supabase_get_first("users", {"id": user_id})
            account_row = _supabase_get_first("bank_accounts", {"user_id": user_id})
            recent_transactions = _get_user_transactions(user_id, limit=10)

            if user_row and user_row.get("full_name"):
                user_name = user_row.get("full_name")
            if account_row:
                account_number = account_row.get("account_number") or ""
                customer_id = account_row.get("customer_id") or ""
                ifsc_code = account_row.get("ifsc_code") or ""
                branch_name = account_row.get("branch_name") or ""
        except Exception as exc:
            print("DASHBOARD LOAD ERROR:", exc)
            recent_transactions = []
    else:
        recent_transactions = []

    return render_template(
        "dashboard.html",
        user_id=user_id,
        user_name=user_name,
        account_number=account_number,
        customer_id=customer_id,
        ifsc_code=ifsc_code,
        branch_name=branch_name,
        recent_transactions=recent_transactions,
    )


@app.route("/view-statement")
def view_statement():
    user_id = (request.args.get("user_id") or "").strip()
    user_name = "Customer"
    account_number = ""
    transactions = []

    if user_id and _supabase_ready():
        try:
            user_row = _supabase_get_first("users", {"id": user_id})
            account_row = _supabase_get_first("bank_accounts", {"user_id": user_id})
            transactions = _get_user_transactions(user_id, limit=200)
            if user_row and user_row.get("full_name"):
                user_name = user_row.get("full_name")
            if account_row:
                account_number = account_row.get("account_number") or ""
        except Exception as exc:
            print("VIEW STATEMENT ERROR:", exc)

    return render_template(
        "statement.html",
        user_id=user_id,
        user_name=user_name,
        account_number=account_number,
        transactions=transactions,
    )


@app.route("/save-onboarding", methods=["POST"])
def save_onboarding():
    try:
        data = request.json or {}
        basic = data.get("basicDetails", {}) or {}
        contact = data.get("contactDetails", {}) or {}
        identity = data.get("identityDetails", {}) or {}
        financial = data.get("financialDetails", {}) or {}
        risk = data.get("riskDetails", {}) or {}
        prefs = data.get("accountPreferences", {}) or {}
        nominee = data.get("nomineeDetails", {}) or {}
        doc_status = data.get("documentVerificationStatus", "pending")

        user_id = _find_or_create_user_id(basic, contact)

        _supabase_upsert_by_user_id(
            "addresses",
            {
                "user_id": user_id,
                "current_address": contact.get("currentAddress"),
                "permanent_address": contact.get("permanentAddress"),
                "city": contact.get("city"),
                "state": contact.get("state"),
                "pincode": contact.get("pincode"),
                "country": contact.get("country"),
            },
        )

        _supabase_upsert_by_user_id(
            "identity_details",
            {
                "user_id": user_id,
                "aadhaar_number": identity.get("aadhaar"),
                "pan_number": identity.get("pan"),
                "pan_verification_status": financial.get("panVerificationStatus"),
            },
        )

        annual_income_value = None
        try:
            annual_income_value = float(str(financial.get("annualIncome", "")).replace(",", ""))
        except Exception:
            annual_income_value = None

        _supabase_upsert_by_user_id(
            "financial_details",
            {
                "user_id": user_id,
                "occupation_type": financial.get("occupationType"),
                "employer_name": financial.get("employerName"),
                "annual_income": annual_income_value,
                "source_of_income": financial.get("sourceIncome"),
                "tax_residency": financial.get("taxResidency"),
                "pan_verification_status": financial.get("panVerificationStatus"),
            },
        )

        _supabase_upsert_by_user_id(
            "risk_assessments",
            {
                "user_id": user_id,
                "investment_experience": risk.get("investmentExperience"),
                "risk_appetite": risk.get("riskAppetite"),
                "investment_goal": risk.get("investmentGoal"),
                "investment_duration": risk.get("investmentDuration"),
            },
        )

        _supabase_upsert_by_user_id(
            "account_preferences",
            {
                "user_id": user_id,
                "account_type": prefs.get("accountType"),
                "debit_card_required": _as_bool(prefs.get("debitCardRequired")),
                "cheque_book_required": _as_bool(prefs.get("chequeBookRequired")),
                "internet_banking": _as_bool(prefs.get("internetBanking")),
                "nominee_required": _as_bool(prefs.get("nomineeRequired")),
                "communication_mode": prefs.get("communicationMode"),
            },
        )

        if _as_bool(prefs.get("nomineeRequired")):
            _supabase_upsert_by_user_id(
                "nominees",
                {
                    "user_id": user_id,
                    "nominee_name": nominee.get("nomineeName"),
                    "relationship": nominee.get("relationship"),
                    "nominee_dob": _normalize_date(nominee.get("nomineeDob", "")) or None,
                    "nominee_address": nominee.get("nomineeAddress"),
                    "nominee_phone": nominee.get("nomineePhone"),
                    "guardian_details": nominee.get("guardianDetails"),
                },
            )

        _supabase_upsert_by_user_id(
            "document_verifications",
            {
                "user_id": user_id,
                "status": "verified" if doc_status == "verified" else "pending",
                "warnings": data.get("documentWarnings", []),
                "extracted_data": data.get("documentExtractedData", {}),
            },
        )

        _supabase_upsert_by_user_id(
            "onboarding_progress",
            {
                "user_id": user_id,
                "current_step": "review_confirm",
                "verification_status": doc_status,
                "completed": False,
            },
        )

        return jsonify({"ok": True, "user_id": user_id})
    except Exception as exc:
        print("SAVE ONBOARDING ERROR:", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/save-bank-account", methods=["POST"])
def save_bank_account():
    try:
        data = request.json or {}
        user_id = data.get("user_id")
        if not user_id:
            return jsonify({"ok": False, "error": "user_id is required"}), 400

        existing = _supabase_get_first("bank_accounts", {"user_id": user_id})
        if existing and existing.get("id"):
            _supabase_patch_by_id("bank_accounts", existing["id"], {"account_status": "active"})
            account_payload = {
                "user_id": user_id,
                "account_number": existing.get("account_number"),
                "customer_id": existing.get("customer_id"),
                "ifsc_code": existing.get("ifsc_code"),
                "branch_name": existing.get("branch_name"),
                "account_status": "active",
            }
        else:
            branches = ["MG Road Branch", "City Center Branch", "Tech Park Branch", "Lake View Branch"]
            account_payload = {
                "user_id": user_id,
                "account_number": _generate_unique_account_number(),
                "customer_id": _generate_unique_customer_id(),
                "ifsc_code": "AIBK0001234",
                "branch_name": random.choice(branches),
                "account_status": "active",
            }
            _supabase_request("POST", "bank_accounts", account_payload)

        _supabase_upsert_by_user_id(
            "onboarding_progress",
            {
                "user_id": user_id,
                "current_step": "account_creation",
                "verification_status": "completed",
                "completed": True,
            },
        )

        return jsonify(
            {
                "ok": True,
                "user_id": user_id,
                "account": {
                    "account_number": account_payload.get("account_number"),
                    "customer_id": account_payload.get("customer_id"),
                    "ifsc_code": account_payload.get("ifsc_code"),
                    "branch_name": account_payload.get("branch_name"),
                },
            }
        )
    except Exception as exc:
        print("SAVE BANK ACCOUNT ERROR:", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/save-login-credentials", methods=["POST"])
def save_login_credentials():
    try:
        data = request.json or {}
        user_id = (data.get("user_id") or "").strip()
        mobile = _normalize_digits(data.get("mobile", ""))
        pin = str(data.get("pin") or "").strip()

        if not user_id:
            return jsonify({"ok": False, "error": "user_id is required"}), 400
        if not re.fullmatch(r"\d{10}", mobile):
            return jsonify({"ok": False, "error": "valid mobile is required"}), 400
        if not re.fullmatch(r"\d{4}|\d{6}", pin):
            return jsonify({"ok": False, "error": "valid pin is required"}), 400

        storage = _save_login_credentials(user_id, mobile, pin)
        return jsonify({"ok": True, "stored_in": storage})
    except Exception as exc:
        print("SAVE LOGIN CREDENTIALS ERROR:", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/login", methods=["POST"])
def login():
    try:
        data = request.json or {}
        mobile = _normalize_digits(data.get("mobile", ""))
        pin = str(data.get("pin") or "").strip()

        if not re.fullmatch(r"\d{10}", mobile):
            return jsonify({"ok": False, "error": "Please enter a valid 10-digit mobile number."}), 400
        if not re.fullmatch(r"\d{4}|\d{6}", pin):
            return jsonify({"ok": False, "error": "Please enter a valid 4/6 digit PIN."}), 400

        user_row = _find_user_by_mobile_and_pin(mobile, pin)
        if not user_row or not user_row.get("id"):
            return jsonify({"ok": False, "error": "Invalid mobile number or PIN."}), 401

        user_id = user_row.get("id")
        return jsonify({"ok": True, "user_id": user_id, "redirect_url": f"/dashboard?user_id={user_id}"})
    except Exception as exc:
        print("LOGIN ERROR:", exc)
        return jsonify({"ok": False, "error": "Login failed. Please try again."}), 500


@app.route("/verify-documents", methods=["POST"])
def verify_documents():
    try:
        details_json = request.form.get("details_json", "{}")
        user_details = json.loads(details_json)

        address_file = request.files.get("address_proof")
        photo_file = request.files.get("photo_upload")
        income_file = request.files.get("income_proof")

        if not address_file or not photo_file:
            return jsonify(
                {
                    "ok": False,
                    "warning": "Address Proof and Photo are required for verification.",
                }
            ), 400

        docs_to_check = [
            ("Address Proof", address_file),
            ("Photo Upload", photo_file),
        ]
        if income_file:
            docs_to_check.append(("Income Proof", income_file))

        doc_payload = []
        for label, fs in docs_to_check:
            doc_payload.append((label, fs.read(), fs.mimetype or "application/octet-stream"))

        extraction_errors = []
        extracted_docs = []
        try:
            extracted_docs = _extract_document_fields_batch(doc_payload)
        except Exception as doc_exc:
            error_text = str(doc_exc)
            print("VERIFY DOC EXTRACT ERROR [batch]:", doc_exc)

            if "RESOURCE_EXHAUSTED" in error_text or "quota" in error_text.lower():
                return jsonify(
                    {
                        "ok": True,
                        "pending": False,
                        "warnings": [],
                        "message": "KYC verified successfully. Next step: Financial status",
                        "extracted": extracted_docs,
                        "extraction_errors": ["quota_exceeded"],
                    }
                ), 200

            extraction_errors.append("batch extraction unavailable")
            extracted_docs = [
                {
                    "source_label": label,
                    "document_type": label,
                    "full_name": "",
                    "dob": "",
                    "aadhaar_number": "",
                    "pan_number": "",
                    "address_line": "",
                    "city": "",
                    "state": "",
                    "pincode": "",
                    "country": "",
                }
                for (label, _bytes, _mime) in doc_payload
            ]

        warnings = _compare_user_vs_extracted(user_details, extracted_docs)
        if extraction_errors:
            warnings.append(
                "AI extraction is temporarily unavailable for one or more documents. Please retry or upload clearer files."
            )
        return jsonify(
            {
                "ok": True,
                "warnings": warnings,
                "extracted": extracted_docs,
                "extraction_errors": extraction_errors,
            }
        )
    except Exception as exc:
        print("VERIFY DOC ERROR:", exc)
        return jsonify(
            {
                "ok": False,
                "warning": "Could not verify documents right now. Please retry.",
                "error": str(exc),
            }
        ), 500


@app.route("/chat", methods=["POST"])
def chat():
    try:
        user_message = request.json.get("message")

        if not user_message:
            return jsonify({"reply": "Please enter something"})

        predefined = get_predefined_reply(user_message)
        if predefined:
            return jsonify({"reply": predefined})

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"""
            You are the AI Bank onboarding assistant shown on a welcome page.
            Keep replies short, clear, and practical.
            If user asks about Login: explain it is for existing account holders.
            If user asks about Get Started: explain it opens a new account.
            Ask one question at a time when guiding account opening.

            User: {user_message}
            """
        )

        return jsonify({"reply": response.text})

    except Exception as e:
        print("ERROR:", e)
        return jsonify({"reply": "AI error, try again"})


@app.route("/dashboard-chat", methods=["POST"])
def dashboard_chat():
    try:
        user_message = (request.json or {}).get("message", "")
        if not user_message:
            return jsonify({"reply": "Please enter your request."})

        predefined = get_dashboard_predefined_reply(user_message)
        if predefined:
            return jsonify({"reply": predefined})

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"""
            You are the AI Bank dashboard assistant.
            Give only practical step-by-step guidance for dashboard actions.
            Keep it concise and actionable.
            If a user asks how to do something, reply with numbered steps.

            User: {user_message}
            """
        )
        return jsonify({"reply": response.text})
    except Exception as exc:
        print("DASHBOARD CHAT ERROR:", exc)
        return jsonify({"reply": "I could not process that right now. Please try again."})


@app.route("/send-money")
def send_money():
    user_id = (request.args.get("user_id") or "").strip()
    return render_template("send_money.html", user_id=user_id)


@app.route("/receive-money")
def receive_money():
    user_id = (request.args.get("user_id") or "").strip()
    return render_template("receive_money.html", user_id=user_id)


@app.route("/send-money/pin")
def send_money_pin():
    user_id = (request.args.get("user_id") or "").strip()
    contact = (request.args.get("contact") or "").strip()
    amount = (request.args.get("amount") or "").strip()
    return render_template("send_money_pin.html", user_id=user_id, contact=contact, amount=amount)


@app.route("/send-money/confirm", methods=["POST"])
def send_money_confirm():
    try:
        data = request.json or {}
        user_id = (data.get("user_id") or "").strip()
        contact = (data.get("contact") or "").strip()
        pin = str(data.get("pin") or "").strip()

        raw_amount = str(data.get("amount") or "").replace(",", "").strip()
        try:
            amount = float(raw_amount)
        except Exception:
            amount = -1

        if not user_id:
            return jsonify({"ok": False, "error": "user_id is required"}), 400
        if not contact:
            return jsonify({"ok": False, "error": "contact is required"}), 400
        if amount <= 0:
            return jsonify({"ok": False, "error": "valid amount is required"}), 400
        if not re.fullmatch(r"\d{4}|\d{6}", pin):
            return jsonify({"ok": False, "error": "Enter a valid PIN."}), 400
        if not _verify_pin_for_user(user_id, pin):
            return jsonify({"ok": False, "error": "Invalid PIN."}), 401

        reference = f"TXN{datetime.utcnow().strftime('%Y%m%d%H%M%S')}{_random_digits(3)}"
        storage = _store_payment_transaction(user_id, contact, amount, reference)
        return jsonify(
            {
                "ok": True,
                "status": "sent",
                "reference": reference,
                "message": "Money sent",
                "stored_in": storage,
            }
        )
    except Exception as exc:
        print("SEND MONEY CONFIRM ERROR:", exc)
        return jsonify({"ok": False, "error": "Could not process transfer right now."}), 500


@app.route("/receive-money/confirm", methods=["POST"])
def receive_money_confirm():
    try:
        data = request.json or {}
        user_id = (data.get("user_id") or "").strip()
        contact = (data.get("contact") or "").strip()

        raw_amount = str(data.get("amount") or "").replace(",", "").strip()
        try:
            amount = float(raw_amount)
        except Exception:
            amount = -1

        if not user_id:
            return jsonify({"ok": False, "error": "user_id is required"}), 400
        if not contact:
            return jsonify({"ok": False, "error": "contact is required"}), 400
        if amount <= 0:
            return jsonify({"ok": False, "error": "valid amount is required"}), 400

        reference = f"RCV{datetime.utcnow().strftime('%Y%m%d%H%M%S')}{_random_digits(3)}"
        storage = _store_payment_transaction(
            user_id=user_id,
            contact=contact,
            amount=amount,
            reference=reference,
            txn_type="credit",
            description=f"Received from {contact}",
            status="success",
        )
        return jsonify(
            {
                "ok": True,
                "status": "received",
                "reference": reference,
                "message": "Money received",
                "stored_in": storage,
            }
        )
    except Exception as exc:
        print("RECEIVE MONEY CONFIRM ERROR:", exc)
        return jsonify({"ok": False, "error": "Could not process receive money right now."}), 500


if __name__ == "__main__":
    app.run(debug=True)