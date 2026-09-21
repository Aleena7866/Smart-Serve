import os
import math
import hmac
import hashlib
import json
import uuid
import secrets
import smtplib
import ssl
import threading
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from datetime import datetime

from dotenv import load_dotenv

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=ENV_FILE, override=False)

try:
    from authlib.integrations.flask_client import OAuth
except ImportError:
    OAuth = None

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    abort,
    send_from_directory
)

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)

from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from flask.sessions import SecureCookieSessionInterface

from database import get_db_connection, init_database
from matching import start_wave, accept_offer, reject_offer, expire_and_requeue, candidates, available_providers, haversine, diagnose

try:
    import razorpay
except ImportError:
    razorpay = None

try:
    from flask_socketio import SocketIO, emit, join_room, leave_room
except ImportError:
    SocketIO = None
    emit = join_room = leave_room = None


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "smart-serve-development-key")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
# Opt-in template auto reload (TEMPLATES_AUTO_RELOAD=1) so template edits show without restarting a non-debug server.
app.config["TEMPLATES_AUTO_RELOAD"] = os.getenv("TEMPLATES_AUTO_RELOAD", "0") == "1"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "0") == "1",
)


class _TransportAwareSessionInterface(SecureCookieSessionInterface):
    """Over HTTPS (incl. reverse proxies / embedded previews) the session cookie must be
    `SameSite=None; Secure`, otherwise browsers drop it inside iframes and the login loops.
    Over plain HTTP (local development) we keep `Lax`, because `None` requires `Secure`."""

    def get_cookie_secure(self, app):
        return bool(request and request.is_secure) or super().get_cookie_secure(app)

    def get_cookie_samesite(self, app):
        if request and request.is_secure:
            return "None"
        return super().get_cookie_samesite(app)


app.session_interface = _TransportAwareSessionInterface()

# ---------------- EMAIL NOTIFICATIONS ----------------
# SMTP is optional: authentication must never fail just because an email could not be sent.
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
try:
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
except ValueError:
    SMTP_PORT = 587
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", SMTP_USERNAME).strip()
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "SmartServe").strip() or "SmartServe"
EMAIL_NOTIFICATIONS_ENABLED = bool(SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD and SMTP_FROM_EMAIL)
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:5000").rstrip("/")


# ---------------- PIN-CODE LOCATION (INDIA) ----------------
def _lookup_pincode_location(pincode):
    """Resolve an Indian 6-digit PIN to an approximate map coordinate.
    This is intentionally an approximate locality position, not a user's exact address.
    """
    pincode = "".join(ch for ch in str(pincode or "") if ch.isdigit())
    if len(pincode) != 6:
        raise ValueError("Enter a valid 6-digit Indian PIN code.")

    # Reliable testing fallback for Bidar PIN used during local development.
    fallback = {
        "585401": {"latitude": 17.9136, "longitude": 77.5199,
                   "label": "Bidar, Karnataka", "pincode": "585401"}
    }

    offices = []
    try:
        req = urllib.request.Request(
            f"https://api.postalpincode.in/pincode/{pincode}",
            headers={"User-Agent": "SmartServe/2.0"}
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload and payload[0].get("Status") == "Success":
            offices = payload[0].get("PostOffice") or []
    except Exception as exc:
        print("PINCODE POSTAL API ERROR:", exc)

    if offices:
        office = offices[0]
        parts = [
            office.get("Name"), office.get("District"),
            office.get("State"), office.get("Pincode"), "India"
        ]
        query = ", ".join(str(x) for x in parts if x)
        try:
            params = urllib.parse.urlencode({
                "q": query, "format": "json", "limit": 1,
                "countrycodes": "in"
            })
            req = urllib.request.Request(
                "https://nominatim.openstreetmap.org/search?" + params,
                headers={"User-Agent": "SmartServe/2.0 (local service marketplace)"}
            )
            with urllib.request.urlopen(req, timeout=8) as response:
                geo = json.loads(response.read().decode("utf-8"))
            if geo:
                return {
                    "latitude": float(geo[0]["lat"]),
                    "longitude": float(geo[0]["lon"]),
                    "label": f"{office.get('District') or office.get('Name')}, {office.get('State')}",
                    "pincode": pincode
                }
        except Exception as exc:
            print("PINCODE GEOCODING ERROR:", exc)

    if pincode in fallback:
        return fallback[pincode]
    raise ValueError("PIN code found, but its map location could not be resolved. Check your internet connection and try again.")

def _email_escape(value):
    import html
    return html.escape(str(value or ""))

def _send_email(to_email, subject, html_body, text_body):
    if not to_email:
        return False
    if not EMAIL_NOTIFICATIONS_ENABLED:
        print("EMAIL NOTIFICATION SKIPPED: SMTP is not configured.")
        return False
    message = EmailMessage()
    message["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(message)
        print(f"EMAIL SENT: {subject} -> {to_email}")
        return True
    except Exception as exc:
        print(f"EMAIL SEND ERROR for {to_email}: {exc}")
        return False

def _send_auth_email(user, event="login", login_method=None):
    name = _email_escape(user["name"])
    email = _email_escape(user["email"])
    role = "Service Provider" if str(user["role"]).lower() == "provider" else "Customer"
    role_plain = "Service Provider" if str(user["role"]).lower() == "provider" else "Customer"
    method = login_method or (user["auth_provider"] if "auth_provider" in user.keys() else "email")
    method = "Google" if str(method).lower() == "google" else "Email & Password"
    now = datetime.now().strftime("%A, %d %B %Y, %I:%M %p")
    if event == "signup":
        title = "You are registered with SmartServe!"
        headline = "Congratulations! Your SmartServe account is ready."
        intro = "Your account has been successfully registered. You can now use SmartServe to book services or offer your professional skills."
        subject = "🎉 Welcome to SmartServe — Registration Successful"
        status_label = "Account Status"
        status_value = "Active • Successfully Registered"
    else:
        title = "Successful login to SmartServe"
        headline = "Congratulations! You have successfully logged in."
        intro = "A successful login to your SmartServe account was completed. Your account is now ready to use."
        subject = "🔐 SmartServe — Successful Login"
        status_label = "Login Status"
        status_value = "Successful • Account Authenticated"
    button_url = f"{APP_BASE_URL}/"
    html_body = f"""<!doctype html><html><body style='margin:0;background:#111827;font-family:Arial,sans-serif;color:#dbe4f0;padding:24px'>
<div style='max-width:680px;margin:auto;background:#0f172a;border:1px solid #334155;border-radius:18px;overflow:hidden'>
<div style='padding:34px 36px;background:linear-gradient(135deg,#4338ca,#0891b2);color:#fff'>
<div style='font-size:14px;font-weight:700;letter-spacing:2px'>🏠 SMARTSERVE</div>
<h1 style='font-size:32px;line-height:1.15;margin:18px 0 10px'>{_email_escape(headline)}</h1>
<p style='font-size:16px;line-height:1.6;margin:0'>{_email_escape(intro)}</p>
</div>
<div style='padding:30px 36px'>
<p style='font-size:18px'>Hello <strong style='color:#38bdf8'>{name}</strong>,</p>
<p style='font-size:15px;line-height:1.7'>Thank you for using SmartServe. Here are the details of this account activity.</p>
<div style='border:1px solid #475569;border-radius:14px;padding:20px;margin:24px 0;background:#172033'>
<div style='font-weight:800;font-size:15px;margin-bottom:16px'>📋 ACCOUNT ACTIVITY DETAILS</div>
<table style='width:100%;border-collapse:collapse;font-size:14px'>
<tr><td style='padding:9px 0;color:#94a3b8'>Name</td><td style='padding:9px 0;font-weight:700'>{name}</td></tr>
<tr><td style='padding:9px 0;color:#94a3b8'>Email</td><td style='padding:9px 0'>{email}</td></tr>
<tr><td style='padding:9px 0;color:#94a3b8'>Account Type</td><td style='padding:9px 0;font-weight:700'>{_email_escape(role)}</td></tr>
<tr><td style='padding:9px 0;color:#94a3b8'>Authentication</td><td style='padding:9px 0'>{_email_escape(method)}</td></tr>
<tr><td style='padding:9px 0;color:#94a3b8'>{_email_escape(status_label)}</td><td style='padding:9px 0;color:#34d399;font-weight:700'>● {_email_escape(status_value)}</td></tr>
<tr><td style='padding:9px 0;color:#94a3b8'>Date &amp; Time</td><td style='padding:9px 0'>{_email_escape(now)}</td></tr>
</table></div>
<div style='text-align:center;margin:30px 0'><a href='{_email_escape(button_url)}' style='display:inline-block;background:linear-gradient(135deg,#4f46e5,#06b6d4);color:#fff;text-decoration:none;font-weight:800;padding:14px 28px;border-radius:10px'>Open SmartServe →</a></div>
<div style='border-left:4px solid #f59e0b;padding:10px 14px;background:#1e293b;font-size:13px;line-height:1.6'><strong>Security notice:</strong> If you did not perform this {"registration" if event == "signup" else "login"}, please secure your account and contact SmartServe support.</div>
<p style='text-align:center;color:#94a3b8;font-size:12px;line-height:1.6;margin-top:30px'>This is an automated SmartServe confirmation email.<br>© 2026 SmartServe • AI-Powered Local Service Marketplace</p>
</div></div></body></html>"""
    text_body = f"SmartServe — {title}\n\nHello {user['name']},\n\n{intro}\n\nName: {user['name']}\nEmail: {user['email']}\nAccount Type: {role_plain}\nAuthentication: {method}\n{status_label}: {status_value}\nDate & Time: {now}\n\nOpen SmartServe: {button_url}\n\nSecurity notice: If you did not perform this {'registration' if event == 'signup' else 'login'}, please secure your account and contact SmartServe support."
    return _send_email(user["email"], subject, html_body, text_body)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "").strip()
GOOGLE_OAUTH_ENABLED = bool(OAuth and GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
oauth = OAuth(app) if OAuth else None
if GOOGLE_OAUTH_ENABLED:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email"},
    )

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
# RAZORPAY_BASE_URL (host only, e.g. http://127.0.0.1:5055) points the SDK at a local
# Razorpay-compatible stub during automated tests. Leave it unset in production (api.razorpay.com).
RAZORPAY_BASE_URL = os.getenv("RAZORPAY_BASE_URL", "").strip()
razorpay_client = (
    razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), **({"base_url": RAZORPAY_BASE_URL} if RAZORPAY_BASE_URL else {}))
    if razorpay and RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET
    else None
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Public media (profile photos, portfolio, request photos) lives OUTSIDE the
# static folder and is served through /media/<file> so that private files
# (completion proofs, verification photos) are never exposed by Flask static.
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
LEGACY_UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
PRIVATE_UPLOAD_FOLDER = os.path.join(BASE_DIR, "private_uploads")
os.makedirs(PRIVATE_UPLOAD_FOLDER, exist_ok=True)
app.config["PRIVATE_UPLOAD_FOLDER"] = PRIVATE_UPLOAD_FOLDER

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
ALLOWED_DOCUMENT_EXTENSIONS = ALLOWED_EXTENSIONS | {"pdf"}
PRIVATE_MEDIA_PREFIXES = ("completion_", "verification_", "ekyc_")

app.config["UPLOAD_FOLDER"] = os.path.abspath(os.getenv("SMARTSERVE_UPLOAD_FOLDER") or UPLOAD_FOLDER)
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024


def _media_dirs():
    """Directories searched for uploaded media (current + legacy static/uploads)."""
    dirs = [app.config["UPLOAD_FOLDER"]]
    if os.path.isdir(LEGACY_UPLOAD_FOLDER) and os.path.abspath(LEGACY_UPLOAD_FOLDER) not in [os.path.abspath(d) for d in dirs]:
        dirs.append(LEGACY_UPLOAD_FOLDER)
    return dirs


def _media_basename(path):
    if not path:
        return None
    name = os.path.basename(str(path).replace("\\", "/").strip())
    if not name or name in (".", "..") or "/" in name:
        return None
    return name


def _find_media(path):
    """Return the absolute file path for a stored media reference, or None."""
    name = _media_basename(path)
    if not name:
        return None
    for directory in _media_dirs():
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def media_url(path, fallback=None):
    """Template helper: public URL for an uploaded image, or fallback when missing."""
    if not path:
        return fallback
    value = str(path)
    if value.startswith("http://") or value.startswith("https://"):
        return value
    name = _media_basename(value)
    if not name or name.lower().startswith(PRIVATE_MEDIA_PREFIXES):
        return fallback
    if _find_media(name):
        return url_for("media_file", filename=name)
    return fallback


app.jinja_env.globals["media_url"] = media_url


@app.route("/media/<path:filename>")
def media_file(filename):
    name = _media_basename(filename)
    if not name or not allowed_file(name) or name.lower().startswith(PRIVATE_MEDIA_PREFIXES):
        abort(404)
    for directory in _media_dirs():
        if os.path.isfile(os.path.join(directory, name)):
            return send_from_directory(directory, name, max_age=86400, conditional=True)
    abort(404)


def _validate_image_upload(file_storage):
    """Ensure an upload is a real JPG/PNG/WEBP image. Returns the extension."""
    if not file_storage or not file_storage.filename:
        raise ValueError("Choose an image to upload.")
    if not allowed_file(file_storage.filename):
        raise ValueError("Only JPG, JPEG, PNG and WEBP images are allowed.")
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    stream = file_storage.stream
    try:
        stream.seek(0)
        if PILImage is not None:
            img = PILImage.open(stream)
            img.verify()
            fmt = (img.format or "").lower()
            if fmt not in ("jpeg", "png", "webp"):
                raise ValueError("Unsupported image format.")
            ext = {"jpeg": "jpg", "png": "png", "webp": "webp"}[fmt]
        else:
            head = stream.read(16)
            if not (head.startswith(b"\xff\xd8") or head.startswith(b"\x89PNG") or head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
                raise ValueError("The file is not a valid image.")
    except ValueError:
        raise
    except Exception:
        raise ValueError("The file could not be read as an image. Upload a JPG, PNG or WEBP photo.")
    finally:
        try:
            stream.seek(0)
        except Exception:
            pass
    return ext


def _save_public_image(file_storage, prefix):
    """Validate and store a public image. Returns the relative DB path 'uploads/<name>'."""
    ext = _validate_image_upload(file_storage)
    filename = secure_filename(f"{prefix}_{uuid.uuid4().hex}.{ext}")
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    file_storage.stream.seek(0)
    file_storage.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
    return "uploads/" + filename


def _delete_media(path):
    target = _find_media(path)
    if target:
        try:
            os.remove(target)
        except OSError:
            pass


def _with_photo_urls(providers):
    for p in providers or []:
        if isinstance(p, dict):
            p["photo_url"] = media_url(p.get("profile_photo_path"))
    return providers


def _grouped_services(connection):
    rows = connection.execute(
        "SELECT id,name,description,category,icon,sort_order FROM services ORDER BY COALESCE(sort_order,100), name"
    ).fetchall()
    groups, order = {}, []
    for row in rows:
        category = row["category"] or "Other Services"
        if category not in groups:
            groups[category] = []
            order.append(category)
        groups[category].append(dict(row))
    return [{"name": category, "services": groups[category]} for category in order]

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading", logger=False, engineio_logger=False) if SocketIO else None

def _request_room(request_id):
    return f"request:{int(request_id)}"

def _request_participants(request_id):
    connection = get_db_connection()
    row = connection.execute("""SELECT sr.customer_id, p.user_id AS provider_user_id FROM service_requests sr LEFT JOIN providers p ON p.id = sr.provider_id WHERE sr.id = ?""", (request_id,)).fetchone()
    connection.close()
    if not row:
        return set()
    ids = {int(row["customer_id"])}
    if row["provider_user_id"] is not None:
        ids.add(int(row["provider_user_id"]))
    return ids

def _emit_request_update(request_id, event="request_update"):
    if not socketio:
        return
    connection = get_db_connection()
    row = connection.execute("""SELECT sr.id, sr.status, sr.provider_id, sr.agreed_amount, sr.payment_status, s.name AS service_name, cu.name AS customer_name, pu.name AS provider_name FROM service_requests sr JOIN services s ON s.id = sr.service_id JOIN users cu ON cu.id = sr.customer_id LEFT JOIN providers p ON p.id = sr.provider_id LEFT JOIN users pu ON pu.id = p.user_id WHERE sr.id = ?""", (request_id,)).fetchone()
    connection.close()
    if row:
        socketio.emit(event, dict(row), to=_request_room(request_id))

if socketio:
    @socketio.on("connect")
    def socket_connect():
        if session.get("user_id"):
            join_room(f"user:{int(session['user_id'])}")
            if session.get("role") == "admin":
                join_room("admins")
            emit("socket_ready", {"user_id": session["user_id"], "role": session.get("role")})

    @socketio.on("join_request")
    def socket_join_request(data):
        if not session.get("user_id"):
            emit("socket_error", {"message": "Login required"})
            return
        try:
            request_id = int((data or {}).get("request_id"))
        except (TypeError, ValueError):
            emit("socket_error", {"message": "Invalid request"})
            return
        if session["user_id"] not in _request_participants(request_id):
            emit("socket_error", {"message": "Not allowed for this request"})
            return
        join_room(_request_room(request_id))
        emit("joined_request", {"request_id": request_id})

    @socketio.on("leave_request")
    def socket_leave_request(data):
        try:
            request_id = int((data or {}).get("request_id"))
        except (TypeError, ValueError):
            return
        leave_room(_request_room(request_id))

@app.template_filter("from_json")
def from_json_filter(value):
    try:
        return json.loads(value) if value else []
    except (TypeError, ValueError):
        return []


# Initialize database
init_database()

# Customer-choice architecture: no automatic provider-offer dispatcher is started.

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def allowed_document(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_DOCUMENT_EXTENSIONS


def _sync_provider_services(connection, provider_id, service_ids):
    """Persist the provider's selected service menu and keep legacy skills in sync."""
    cleaned=[]
    for value in service_ids or []:
        try:
            sid=int(value)
        except (TypeError, ValueError):
            continue
        if sid>0 and sid not in cleaned:
            cleaned.append(sid)
    rows=[]
    if cleaned:
        placeholders=','.join('?' for _ in cleaned)
        rows=connection.execute(
            f"SELECT id,name FROM services WHERE id IN ({placeholders}) ORDER BY name", cleaned
        ).fetchall()
    connection.execute("DELETE FROM provider_services WHERE provider_id=?", (provider_id,))
    for row in rows:
        connection.execute("INSERT OR IGNORE INTO provider_services(provider_id,service_id) VALUES(?,?)", (provider_id,row['id']))
    skills=', '.join(row['name'] for row in rows)
    connection.execute("UPDATE providers SET skills=? WHERE id=?", (skills, provider_id))
    return rows


# ---------------- HOME ----------------

@app.route("/")
def home():
    connection = get_db_connection()
    services = connection.execute("SELECT id,name,icon,category,description FROM services ORDER BY COALESCE(sort_order,100), name").fetchall()
    counts = {
        "providers": connection.execute("SELECT COUNT(*) c FROM providers WHERE approved=1").fetchone()["c"],
        "completed": connection.execute("SELECT COUNT(*) c FROM service_requests WHERE status='COMPLETED'").fetchone()["c"],
        "reviews": connection.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"],
    }
    connection.close()
    return render_template("index.html", services=services, counts=counts)


@app.route("/health")
def health():
    try:
        connection = get_db_connection()
        connection.execute("SELECT 1").fetchone()
        connection.close()
        return jsonify({"status": "ok", "service": "smartserve"})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 503


def _start_session(user):
    session.clear()
    session["user_id"] = user["id"]
    session["user_name"] = user["name"]
    session["role"] = user["role"]
    session["email"] = user["email"]
    session["auth_provider"] = user["auth_provider"] if "auth_provider" in user.keys() else "local"
    session["profile_photo_path"] = user["profile_photo_path"] if "profile_photo_path" in user.keys() else None


def _dashboard_for_role():
    role = session.get("role")
    if role == "admin":
        return redirect(url_for("admin_operations"))
    return redirect(url_for("provider_dashboard" if role == "provider" else "customer_dashboard"))


@app.context_processor
def inject_auth_state():
    preferred_language = "en"
    low_bandwidth = False
    if session.get("user_id"):
        try:
            c = get_db_connection(); u = c.execute("SELECT preferred_language,low_bandwidth_mode FROM users WHERE id=?", (session["user_id"],)).fetchone(); c.close()
            if u:
                preferred_language = u["preferred_language"] or "en"
                low_bandwidth = bool(u["low_bandwidth_mode"])
        except Exception:
            pass
    return {"google_oauth_enabled": GOOGLE_OAUTH_ENABLED, "preferred_language": preferred_language, "low_bandwidth": low_bandwidth}


@app.route("/api/system/diagnostic")
def system_diagnostic():
    if "user_id" not in session:
        return jsonify(success=False,message="Login required"),401
    conn=get_db_connection()
    tables={
        "users":["id","name","email","role","phone","profile_photo_path","pincode","is_online"],
        "providers":["id","user_id","skills","bio","ekyc_document_path","ekyc_status"],
        "provider_services":["provider_id","service_id"],
        "service_requests":["id","customer_id","provider_id","status","completion_proof_paths","confirmation_code"],
        "messages":["request_id","sender_id","message"],
        "reviews":["provider_id","customer_id","rating"],
    }
    result={}
    for table,needed in tables.items():
        try:
            cols={r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            result[table]={"ok":all(c in cols for c in needed),"missing":[c for c in needed if c not in cols]}
        except Exception as exc:
            result[table]={"ok":False,"missing":needed,"error":str(exc)}
    journal=conn.execute("PRAGMA journal_mode").fetchone()[0]
    busy=conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()
    return jsonify(success=True,database=os.path.abspath(__import__('database').DATABASE),journal_mode=journal,busy_timeout_ms=busy,tables=result)

# ---------------- GOOGLE OAUTH ----------------

@app.route("/auth/google")
def google_login():
    if not GOOGLE_OAUTH_ENABLED:
        flash("Google OAuth is not configured yet. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to .env.")
        return redirect(url_for("login"))
    redirect_uri = GOOGLE_REDIRECT_URI or url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def google_callback():
    if not GOOGLE_OAUTH_ENABLED:
        flash("Google OAuth is not configured.")
        return redirect(url_for("login"))
    try:
        token = oauth.google.authorize_access_token()
        userinfo = token.get("userinfo") or {}
        email = str(userinfo.get("email", "")).strip().lower()
        name = str(userinfo.get("name", "Smart Serve User")).strip() or "Smart Serve User"
        google_sub = str(userinfo.get("sub", "")).strip()
        email_verified = bool(userinfo.get("email_verified", False))
        if not email or not google_sub or not email_verified:
            flash("Google account email could not be verified.")
            return redirect(url_for("login"))

        connection = get_db_connection()
        user = connection.execute("SELECT * FROM users WHERE google_sub = ? OR LOWER(email) = ?", (google_sub, email)).fetchone()
        if user:
            # Link an existing local account to the verified Google identity.
            connection.execute("UPDATE users SET google_sub = ?, auth_provider = 'google', email_verified = 1 WHERE id = ?", (google_sub, user["id"]))
            connection.commit()
            user = connection.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
            connection.close()
            _start_session(user)
            _send_auth_email(user, event="login", login_method="google")
            flash("🎉 Congratulations! You have successfully logged in with Google.")
            return _dashboard_for_role()

        # New Google users choose Customer or Provider before the account is created.
        connection.close()
        session["google_pending"] = {"sub": google_sub, "email": email, "name": name}
        return redirect(url_for("google_complete"))
    except Exception as exc:
        print("GOOGLE OAUTH ERROR:", exc)
        flash("Google sign-in failed. Check your OAuth client and redirect URI.")
        return redirect(url_for("login"))


@app.route("/auth/google/complete", methods=["GET", "POST"])
def google_complete():
    pending = session.get("google_pending")
    if not pending:
        return redirect(url_for("login"))
    if request.method == "POST":
        role = request.form.get("role", "").strip().lower()
        service_ids = request.form.getlist("service_ids")
        skills = request.form.get("skills", "").strip()
        try:
            experience = max(0, int(request.form.get("experience", "0") or 0))
        except ValueError:
            experience = 0
        if role not in {"customer", "provider"}:
            flash("Choose Customer or Provider.")
            return redirect(url_for("google_complete"))
        connection = get_db_connection()
        try:
            # Password remains non-null for compatibility with existing schema; Google users
            # receive a random unusable password hash and authenticate through Google.
            password_hash = generate_password_hash(secrets.token_urlsafe(32))
            cur = connection.cursor()
            cur.execute("""INSERT INTO users (name,email,password,role,google_sub,auth_provider,email_verified) VALUES (?,?,?,?,?,?,1)""", (pending["name"], pending["email"], password_hash, role, pending["sub"], "google"))
            user_id = cur.lastrowid
            if role == "provider":
                cur.execute("INSERT INTO providers (user_id,skills,experience,rating,approved) VALUES (?,?,?,?,1)", (user_id, skills, experience, 0))
                provider_id=cur.lastrowid
                selected=_sync_provider_services(connection, provider_id, service_ids)
                if not selected and skills:
                    legacy=[]
                    for token in skills.replace(';', ',').replace('|', ',').split(','):
                        rr=cur.execute("SELECT id FROM services WHERE LOWER(name)=LOWER(?)", (token.strip(),)).fetchone()
                        if rr: legacy.append(rr['id'])
                    if legacy: _sync_provider_services(connection, provider_id, legacy)
            connection.commit()
            user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        except Exception as exc:
            connection.rollback()
            print("GOOGLE ACCOUNT CREATE ERROR:", exc)
            flash("Could not create the Google account. The email may already be registered.")
            connection.close()
            return redirect(url_for("login"))
        connection.close()
        session.pop("google_pending", None)
        _start_session(user)
        _send_auth_email(user, event="signup", login_method="google")
        flash("🎉 Congratulations! Your SmartServe account was created and you have successfully logged in with Google.")
        return _dashboard_for_role()
    connection=get_db_connection()
    services=connection.execute("SELECT id,name,description,category,icon FROM services ORDER BY COALESCE(sort_order,100), name").fetchall()
    service_groups=_grouped_services(connection)
    connection.close()
    return render_template("oauth_complete.html", pending=pending, services=services, service_groups=service_groups)


# ---------------- REGISTER ----------------

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "").strip().lower()
        phone = request.form.get("phone", "").strip()

        if not name or not email or not password:
            flash("Please fill in your name, email and password.")
            return redirect(url_for("register"))

        if len(password) < 6:
            flash("Password must be at least 6 characters long.")
            return redirect(url_for("register"))

        if role not in ["customer", "provider"]:
            flash("Please choose whether you are a Customer or a Service Provider.")
            return redirect(url_for("register"))

        if role == "provider" and not request.form.getlist("service_ids") and not request.form.get("skills", "").strip():
            flash("Select at least one service you offer.")
            return redirect(url_for("register"))

        hashed_password = generate_password_hash(password)

        connection = get_db_connection()

        try:

            cursor = connection.cursor()

            cursor.execute("""
                INSERT INTO users (name, email, password, role, phone)
                VALUES (?, ?, ?, ?, ?)
            """, (name, email, hashed_password, role, phone or None))

            user_id = cursor.lastrowid

            # Create provider profile
            if role == "provider":

                service_ids = request.form.getlist("service_ids")
                skills = request.form.get("skills", "").strip()
                try:
                    experience = max(0, min(60, int(request.form.get("experience", 0) or 0)))
                except (TypeError, ValueError):
                    experience = 0

                cursor.execute("""
                    INSERT INTO providers
                    (user_id, skills, experience, rating, approved)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    user_id,
                    skills,
                    experience,
                    0,
                    1
                ))
                provider_id = cursor.lastrowid
                selected_rows = _sync_provider_services(connection, provider_id, service_ids)
                if not selected_rows and skills:
                    # Backward compatibility for old forms: import comma-separated service names.
                    legacy_ids=[]
                    for token in skills.replace(';', ',').replace('|', ',').split(','):
                        row = cursor.execute("SELECT id FROM services WHERE LOWER(name)=LOWER(?)", (token.strip(),)).fetchone()
                        if row: legacy_ids.append(row['id'])
                    if legacy_ids:
                        _sync_provider_services(connection, provider_id, legacy_ids)

            connection.commit()
            user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            try:
                _send_auth_email(user, event="signup", login_method="email")
            except Exception as email_exc:
                print("REGISTRATION EMAIL WARNING:", repr(email_exc))

            flash("Your SmartServe account is ready. Sign in to continue.")
            return redirect(url_for("login"))

        except Exception as e:

            connection.rollback()

            print("REGISTRATION ERROR:", repr(e))
            if "UNIQUE constraint failed" in str(e):
                flash("This email is already registered. Sign in instead or use a different email.")
            else:
                flash("Registration could not be completed. Please try again.")

            return redirect(url_for("register"))

        finally:
            connection.close()

    connection = get_db_connection()
    services = connection.execute("SELECT id,name,description,category,icon FROM services ORDER BY COALESCE(sort_order,100), name").fetchall()
    service_groups = _grouped_services(connection)
    connection.close()
    return render_template("register.html", services=services, service_groups=service_groups)


# ---------------- LOGIN ----------------

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form["email"].strip().lower()
        password = request.form["password"]

        connection = get_db_connection()

        user = connection.execute("""
            SELECT *
            FROM users
            WHERE email = ?
        """, (email,)).fetchone()

        connection.close()

        valid_password = False
        if user:
            try:
                valid_password = check_password_hash(user["password"], password)
            except ValueError:
                # Backward compatibility for older demo databases that stored seed passwords in plaintext.
                valid_password = secrets.compare_digest(str(user["password"]), password)
                if valid_password:
                    connection = get_db_connection()
                    connection.execute("UPDATE users SET password = ? WHERE id = ?", (generate_password_hash(password), user["id"]))
                    connection.commit()
                    connection.close()
        if valid_password:
            _start_session(user)
            _send_auth_email(user, event="login", login_method="email")
            flash("🎉 Congratulations! You have successfully logged in to SmartServe.")
            return _dashboard_for_role()

        flash("Invalid email or password.")

    return render_template("login.html")


# ---------------- LOGOUT ----------------

@app.route("/logout")
def logout():
    if session.get("user_id"):
        try:
            connection = get_db_connection()
            connection.execute("UPDATE users SET is_online = 0 WHERE id = ?", (session["user_id"],))
            connection.commit()
            connection.close()
        except Exception as exc:
            print("LOGOUT PRESENCE ERROR:", exc)
    session.clear()

    return redirect(url_for("home"))


# ---------------- CUSTOMER DASHBOARD ----------------

@app.route("/customer/dashboard")
def customer_dashboard():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session["role"] != "customer":
        return redirect(url_for("login"))

    connection = get_db_connection()

    requests = connection.execute("""
        SELECT
            service_requests.*,
            services.name AS service_name,
            provider_users.name AS provider_name,
            provider_users.phone AS provider_phone,
            provider_users.profile_photo_path AS provider_photo,
            provider_users.latitude AS provider_latitude,
            provider_users.longitude AS provider_longitude,
            provider_users.location_updated_at AS provider_location_updated_at,
            providers.rating AS provider_rating,
            provider_users.is_online AS provider_online,
            customer_users.latitude AS customer_latitude,
            customer_users.longitude AS customer_longitude,
            customer_users.location_updated_at AS customer_location_updated_at
        FROM service_requests
        JOIN services
            ON service_requests.service_id = services.id
        JOIN users AS customer_users
            ON service_requests.customer_id = customer_users.id
        LEFT JOIN providers
            ON service_requests.provider_id = providers.id
        LEFT JOIN users AS provider_users
            ON providers.user_id = provider_users.id
        WHERE service_requests.customer_id = ?
        ORDER BY service_requests.created_at DESC
    """, (
        session["user_id"],
    )).fetchall()

    top_providers = connection.execute("""
        SELECT p.id AS provider_id, p.rating, p.skills, p.experience, u.name, u.is_online, u.profile_photo_path,
               (SELECT COUNT(*) FROM reviews rv WHERE rv.provider_id=p.id) AS review_count
        FROM providers p JOIN users u ON u.id = p.user_id
        WHERE p.approved = 1 ORDER BY p.rating DESC, p.experience DESC LIMIT 4
    """).fetchall()
    services = connection.execute("SELECT id,name,icon,category FROM services ORDER BY COALESCE(sort_order,100), name").fetchall()
    upcoming_plans = connection.execute("""
        SELECT rb.id, rb.frequency, rb.next_run_at, s.name AS service_name, pu.name AS provider_name, rb.provider_id,
               CAST(julianday(rb.next_run_at) - julianday('now') AS INTEGER) AS days_left
        FROM recurring_bookings rb JOIN services s ON s.id = rb.service_id
        LEFT JOIN providers p ON p.id = rb.provider_id LEFT JOIN users pu ON pu.id = p.user_id
        WHERE rb.customer_id = ? AND rb.active = 1 ORDER BY rb.next_run_at ASC LIMIT 5
    """, (session["user_id"],)).fetchall()
    connection.close()

    return render_template(
        "customer_dashboard.html",
        requests=requests, top_providers=top_providers, services=services, upcoming_plans=upcoming_plans
    )

# ---------------- PROVIDER DASHBOARD ----------------

@app.route("/provider/dashboard")
def provider_dashboard():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session["role"] != "provider":
        return redirect(url_for("login"))

    connection = get_db_connection()

    provider = connection.execute("""
        SELECT p.*, u.name, u.email, u.phone, u.profile_photo_path, u.pincode, u.location_source,
               u.is_online, u.latitude, u.longitude, u.location_updated_at
        FROM providers p JOIN users u ON u.id = p.user_id
        WHERE p.user_id = ?
    """, (session["user_id"],)).fetchone()

    requests = []
    offers = []
    provider_reviews = []
    provider_services = []
    stats = {"active": 0, "completed": 0, "pending": 0, "net_earnings": 0.0, "review_count": 0}

    if provider:
        offers = connection.execute("""
            SELECT mo.*, sr.description, sr.estimated_price, sr.upfront_min, sr.upfront_max,
                   sr.service_radius_km, sr.customer_latitude, sr.customer_longitude,
                   s.name AS service_name, cu.name AS customer_name
            FROM match_offers mo
            JOIN service_requests sr ON sr.id=mo.request_id
            JOIN services s ON s.id=sr.service_id
            JOIN users cu ON cu.id=sr.customer_id
            WHERE mo.provider_id=? AND mo.status='OFFERED'
            ORDER BY mo.distance_km ASC, mo.expires_at ASC
        """,(provider["id"],)).fetchall()

        requests = connection.execute("""
            SELECT
                service_requests.*,
                services.name AS service_name,
                customer_users.name AS customer_name,
                customer_users.phone AS customer_phone,
                customer_users.latitude AS customer_latitude,
                customer_users.longitude AS customer_longitude,
                customer_users.location_updated_at AS customer_location_updated_at,
                provider_users.latitude AS provider_latitude,
                provider_users.longitude AS provider_longitude,
                provider_users.location_updated_at AS provider_location_updated_at
            FROM service_requests
            JOIN services
                ON service_requests.service_id = services.id
            JOIN users AS customer_users
                ON service_requests.customer_id = customer_users.id
            JOIN providers AS p
                ON service_requests.provider_id = p.id
            JOIN users AS provider_users
                ON p.user_id = provider_users.id
            WHERE service_requests.provider_id = ?
            ORDER BY service_requests.created_at DESC
        """, (provider["id"],)).fetchall()

        provider_reviews = connection.execute("""
            SELECT r.rating, r.review, r.created_at,
                   u.name AS customer_name,
                   s.name AS service_name
            FROM reviews r
            JOIN users u ON u.id = r.customer_id
            JOIN service_requests sr ON sr.id = r.request_id
            JOIN services s ON s.id = sr.service_id
            WHERE r.provider_id = ?
            ORDER BY r.created_at DESC
            LIMIT 50
        """, (provider["id"],)).fetchall()

        provider_services = connection.execute("""
            SELECT s.id, s.name, s.icon, s.category FROM provider_services ps JOIN services s ON s.id = ps.service_id
            WHERE ps.provider_id = ? ORDER BY COALESCE(s.sort_order,100), s.name
        """, (provider["id"],)).fetchall()
        stats["active"] = sum(1 for r in requests if r["status"] in TRACKING_ACTIVE_STATUSES)
        stats["completed"] = sum(1 for r in requests if r["status"] == "COMPLETED")
        stats["pending"] = sum(1 for r in requests if r["status"] == "ASSIGNED")
        stats["review_count"] = len(provider_reviews)
        earn = connection.execute("SELECT COALESCE(SUM(net_amount),0) net FROM provider_earnings WHERE provider_id=?", (provider["id"],)).fetchone()
        stats["net_earnings"] = float(earn["net"] or 0)

    connection.close()

    return render_template(
        "provider_dashboard.html",
        provider=provider,
        requests=requests,
        offers=offers,
        provider_reviews=provider_reviews,
        provider_services=provider_services,
        stats=stats
    )

# ---------------- SERVICE REQUEST ----------------

@app.route("/request-service", methods=["GET", "POST"])
def request_service():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session["role"] != "customer":
        return redirect(url_for("login"))

    connection = get_db_connection()

    services = connection.execute("""
        SELECT MIN(id) AS id, name, description, MIN(icon) AS icon, MIN(category) AS category, MIN(COALESCE(sort_order,100)) AS sort_order
        FROM services
        GROUP BY LOWER(name)
        ORDER BY sort_order, name
    """).fetchall()

    service_map = {service["name"]: service["id"] for service in services}

    connection.close()

    preselected_service = request.args.get("service", "").strip()
    ai_request_id = request.args.get("request_id", type=int)
    loaded_ai_analysis = None
    loaded_request_id = None
    selected_service_name = preselected_service or None
    if ai_request_id:
        loaded = get_db_connection()
        loaded_row = loaded.execute("""SELECT sr.*, s.name AS service_name FROM service_requests sr JOIN services s ON s.id = sr.service_id WHERE sr.id = ? AND sr.customer_id = ?""", (ai_request_id, session["user_id"])).fetchone()
        loaded.close()
        if loaded_row:
            loaded_request_id = loaded_row["id"]
            selected_service_name = loaded_row["service_name"]
            loaded_ai_analysis = {
                "problem": loaded_row["ai_problem"] or loaded_row["ai_analysis"] or "",
                "possible_cause": loaded_row["ai_possible_cause"] or "",
                "difficulty": loaded_row["ai_difficulty"] or "",
                "recommended_service": loaded_row["ai_recommended_service"] or "",
                "estimated_price": loaded_row["estimated_price"] or "",
                "safety_note": loaded_row["ai_safety_note"] or "",
            }

    if request.method == "POST":

        creation_mode = request.form.get("creation_mode", "ai").strip().lower()
        service_id = request.form.get("service_id")
        description = request.form.get("description", "").strip()
        quick_option = request.form.get("quick_option", "").strip()
        customer_estimate = request.form.get("customer_estimate", "").strip()
        pickup_address = request.form.get("pickup_address", "").strip()
        drop_address = request.form.get("drop_address", "").strip()
        delivery_notes = request.form.get("delivery_notes", "").strip()
        scheduled_at = request.form.get("scheduled_at", "").strip() or None
        customer_pincode = "".join(ch for ch in request.form.get("pincode", "") if ch.isdigit())

        if creation_mode not in {"ai", "quick"}:
            flash("Invalid service creation mode.")
            return redirect(url_for("request_service"))

        if creation_mode == "quick":
            # Quick booking deliberately does not call Gemini. A customer
            # can create a request from a service category/sub-option alone.
            category = request.form.get("service_name", "").strip()
            service_id = service_map.get(category)

            if not service_id:
                flash("Please select a valid service category.")
                return redirect(url_for("request_service"))

            if not quick_option:
                flash("Please select what you need help with.")
                return redirect(url_for("request_service"))

            description = quick_option
            if request.form.get("quick_notes", "").strip():
                description += " — " + request.form.get("quick_notes", "").strip()

        if not service_id:
            flash("Please select a service.")
            return redirect(url_for("request_service"))

        if creation_mode == "ai" and not description:
            flash("Please describe your problem for AI analysis.")
            return redirect(url_for("request_service"))

        # Get selected service
        connection = get_db_connection()

        service = connection.execute("""
            SELECT *
            FROM services
            WHERE id = ?
        """, (service_id,)).fetchone()

        connection.close()

        if not service:
            flash("Invalid service selected.")
            return redirect(url_for("request_service"))

        if not customer_pincode:
            flash("Enter your 6-digit PIN code so SmartServe can place you on the map.")
            return redirect(url_for("request_service"))
        try:
            pin_location = _lookup_pincode_location(customer_pincode)
        except ValueError:
            # Rural/offline fallback: a valid Indian PIN is still a service area even
            # when geocoding is temporarily unavailable. Same-PIN matching works without coordinates.
            pin_location = {"latitude": None, "longitude": None, "label": f"PIN {customer_pincode} service area", "pincode": customer_pincode}

        # Store the selected PIN location on the customer account for local testing
        # and for the real-time matching engine. It is an approximate locality point.
        pin_connection = get_db_connection()
        pin_connection.execute("""
            UPDATE users SET latitude=?, longitude=?, location_accuracy=1000,
                location_updated_at=CURRENT_TIMESTAMP, pincode=?, location_source='PINCODE'
            WHERE id=?
        """, (pin_location["latitude"], pin_location["longitude"], customer_pincode, session["user_id"]))
        pin_connection.commit(); pin_connection.close()

        # ---------------- IMAGE UPLOAD ----------------

        image = request.files.get("image")

        image_path = None
        image_display_path = None

        if image and image.filename:
            try:
                image_display_path = _save_public_image(image, "request")
            except ValueError as exc:
                flash(str(exc))
                return redirect(url_for("request_service"))
            image_path = os.path.join(app.config["UPLOAD_FOLDER"], os.path.basename(image_display_path))

        # ---------------- OPTIONAL AI ANALYSIS ----------------

        ai_result = None

        if creation_mode == "ai":
            from ai_service import analyze_problem

            try:

                ai_result = analyze_problem(
                    service["name"],
                    description,
                    image_path
                )

            except Exception as e:

                print("Gemini error:", e)

                flash(
                    "AI analysis failed. Please try again."
                )

                return redirect(
                    url_for("request_service")
                )

        else:
            # Quick booking has no Gemini dependency. The customer's
            # estimate is optional and can be left blank.
            ai_result = None

        # ---------------- SAVE REQUEST ----------------

        connection = get_db_connection()

        cursor = connection.cursor()

        if creation_mode == "quick":
            estimated_price = None
            if customer_estimate:
                try:
                    estimate_value = float(customer_estimate)
                    if estimate_value < 0 or estimate_value > 1000000:
                        raise ValueError
                    estimated_price = f"₹{estimate_value:,.2f}"
                except ValueError:
                    connection.close()
                    flash("Estimated budget must be a valid amount or left blank.")
                    return redirect(url_for("request_service"))

            cursor.execute("""
                INSERT INTO service_requests
                (
                    customer_id, service_id, description, image_path,
                    ai_analysis, estimated_price, status, booking_mode,
                    pickup_address, drop_address, delivery_notes, scheduled_at,
                    customer_pincode, customer_latitude, customer_longitude, customer_location_accuracy, customer_location_source
                )
                VALUES (?, ?, ?, ?, NULL, ?, 'PENDING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session["user_id"],
                service_id,
                description,
                image_display_path,
                estimated_price,
                'DELIVERY' if service['name'] == 'Delivery & Errands' else 'ON_DEMAND',
                pickup_address or None, drop_address or None, delivery_notes or None, scheduled_at,
                customer_pincode, pin_location["latitude"], pin_location["longitude"], 1000, 'PINCODE'
            ))
        else:
            cursor.execute("""
                INSERT INTO service_requests
                (
                    customer_id, service_id, description, image_path,
                    ai_analysis, estimated_price, status,
                    ai_problem, ai_possible_cause, ai_difficulty,
                    ai_recommended_service, ai_safety_note, scheduled_at,
                    customer_pincode, customer_latitude, customer_longitude, customer_location_accuracy, customer_location_source
                )
                VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                session["user_id"], service_id, description, image_display_path,
                ai_result["problem"], ai_result["estimated_price"],
                ai_result["problem"], ai_result["possible_cause"],
                ai_result["difficulty"], ai_result["recommended_service"],
                ai_result["safety_note"], scheduled_at, customer_pincode,
                pin_location["latitude"], pin_location["longitude"], 1000, 'PINCODE'
            ))

        request_id = cursor.lastrowid

        # Any due repeat plan for this service rolls forward to its next visit.
        for plan in connection.execute("SELECT id, frequency FROM recurring_bookings WHERE customer_id=? AND service_id=? AND active=1 AND datetime(next_run_at) <= datetime('now','+3 day')", (session["user_id"], service_id)).fetchall():
            step = REPEAT_FREQUENCIES.get(str(plan["frequency"]).upper(), '+30 day')
            connection.execute("UPDATE recurring_bookings SET next_run_at=datetime('now', ?) WHERE id=?", (step, plan["id"]))

        connection.commit()

        connection.close()

        # Scheduled jobs are stored first and discovered when the scheduled time arrives.
        if scheduled_at:
            connection = get_db_connection(); connection.execute("UPDATE service_requests SET booking_mode='SCHEDULED',status='SCHEDULED' WHERE id=?",(request_id,)); connection.commit(); connection.close()
        # Go to provider matching
        if creation_mode == "quick":
            flash(f"{service['name']} request created. " + ("Your scheduled service is saved." if scheduled_at else "SmartServe is ready to find nearby providers."))
            return redirect(url_for("searching", request_id=request_id) if not scheduled_at else url_for("customer_dashboard"))

        return render_template(
            "request_service.html",
            services=services,
            service_map=service_map,
            ai_analysis=ai_result,
            request_id=request_id,
            selected_service=service["name"]
        )

    return render_template(
        "request_service.html",
        services=services,
        service_map=service_map,
        ai_analysis=loaded_ai_analysis,
        request_id=loaded_request_id,
        selected_service=selected_service_name
    )



# ---------------- AI ANALYSIS API ----------------

@app.route("/api/ai/analyze-and-create", methods=["POST"])
def ai_analyze_and_create():
    if "user_id" not in session or session.get("role") != "customer":
        return jsonify({"success": False, "message": "Customer login required."}), 401

    service_id = request.form.get("service_id", type=int)
    description = request.form.get("description", "").strip()
    customer_pincode = "".join(ch for ch in request.form.get("pincode", "") if ch.isdigit())
    if not customer_pincode:
        return jsonify({"success": False, "message": "Enter your 6-digit PIN code before AI analysis."}), 400
    try:
        pin_location = _lookup_pincode_location(customer_pincode)
    except ValueError:
        pin_location = {"latitude": None, "longitude": None, "label": f"PIN {customer_pincode} service area", "pincode": customer_pincode}
    if not service_id or not description:
        return jsonify({"success": False, "message": "Select a service and describe the problem."}), 400

    connection = get_db_connection()
    service = connection.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
    connection.close()
    if not service:
        return jsonify({"success": False, "message": "Invalid service selected."}), 400

    image = request.files.get("image")
    image_path = None
    image_display_path = None
    if image and image.filename:
        try:
            image_display_path = _save_public_image(image, "request")
        except ValueError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        image_path = os.path.join(app.config["UPLOAD_FOLDER"], os.path.basename(image_display_path))

    try:
        from ai_service import analyze_problem
        ai_result = analyze_problem(service["name"], description, image_path)
    except Exception as exc:
        print("GEMINI API ERROR:", repr(exc))
        return jsonify({"success": False, "message": str(exc)}), 502

    connection = get_db_connection()
    cur = connection.cursor()
    cur.execute("""
        INSERT INTO service_requests
        (customer_id, service_id, description, image_path, ai_analysis, estimated_price, status,
         ai_problem, ai_possible_cause, ai_difficulty, ai_recommended_service, ai_safety_note,
         customer_pincode, customer_latitude, customer_longitude, customer_location_accuracy, customer_location_source)
        VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        session["user_id"], service_id, description, image_display_path,
        ai_result["problem"], ai_result["estimated_price"],
        ai_result["problem"], ai_result["possible_cause"], ai_result["difficulty"],
        ai_result["recommended_service"], ai_result["safety_note"],
        customer_pincode, pin_location["latitude"], pin_location["longitude"], 1000, 'PINCODE'
    ))
    request_id = cur.lastrowid
    connection.commit()
    connection.close()
    return jsonify({
        "success": True,
        "request_id": request_id,
        "service": service["name"],
        "analysis": ai_result,
        "searching_url": url_for("searching", request_id=request_id)
    })


@app.route("/api/ai/status")
def ai_status():
    key = os.getenv("GEMINI_API_KEY", "").strip()
    configured = bool(key and not key.startswith("YOUR_") and not key.startswith("your_"))
    return jsonify({"configured": configured, "model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash")})



# ---------------- V2 BOOKING / DISPATCH ----------------

def _log_event(connection, request_id, actor_user_id, event_type, message="", metadata=None):
    connection.execute("""
        INSERT INTO service_events(request_id,actor_user_id,event_type,message,metadata_json)
        VALUES(?,?,?,?,?)
    """,(request_id,actor_user_id,event_type,message,json.dumps(metadata or {})))

def _quote_for_request(connection, request_id):
    row=connection.execute("""
        SELECT sr.*, sp.base_price,sp.min_price,sp.max_price,sp.included_km,sp.per_km,sp.platform_fee
        FROM service_requests sr
        LEFT JOIN service_pricing sp ON sp.service_id=sr.service_id
        WHERE sr.id=?
    """,(request_id,)).fetchone()
    if not row:
        return None
    base=float(row["base_price"] or 299)
    minp=float(row["min_price"] or 199)
    maxp=float(row["max_price"] or 1499)
    included=float(row["included_km"] or 3)
    perkm=float(row["per_km"] or 12)
    fee=float(row["platform_fee"] or 20)
    distance=0.0
    if row["customer_latitude"] is not None and row["customer_longitude"] is not None:
        # The quote is based on the customer's selected location and a typical nearby visit.
        # Provider-specific driving distance is shown later and can add a travel adjustment.
        distance=0.0
    travel=max(0.0,distance-included)*perkm
    difficulty=(row["ai_difficulty"] or "").lower()
    multiplier={"easy":0.9,"medium":1.0,"hard":1.35}.get(difficulty,1.0)
    raw=base*multiplier+travel+fee
    low=max(minp, round(raw*0.85))
    high=min(maxp, round(raw*1.25))
    return {"min":low,"max":high,"platform_fee":fee,"travel_fee":round(travel,2),"currency":"INR"}

def _serialize_request_status(connection, request_id, viewer_id):
    row=connection.execute("""
        SELECT sr.*,s.name AS service_name,cu.name AS customer_name,
               p.id AS provider_id,pu.name AS provider_name,pu.phone AS provider_phone,
               pu.latitude AS provider_latitude,pu.longitude AS provider_longitude,
               pu.location_updated_at AS provider_location_updated_at,
               p.rating AS provider_rating
        FROM service_requests sr
        JOIN services s ON s.id=sr.service_id
        JOIN users cu ON cu.id=sr.customer_id
        LEFT JOIN providers p ON p.id=sr.provider_id
        LEFT JOIN users pu ON pu.id=p.user_id
        WHERE sr.id=?
    """,(request_id,)).fetchone()
    if not row or int(row["customer_id"])!=int(viewer_id):
        # provider may also view their own request
        provider_ok=connection.execute("""SELECT 1 FROM providers WHERE id=? AND user_id=?""",(row["provider_id"] if row else -1,viewer_id)).fetchone()
        if not provider_ok:
            return None
    data=dict(row)
    offers=connection.execute("""
        SELECT mo.id,mo.provider_id,mo.wave,mo.status,mo.offered_at,mo.expires_at,
               mo.distance_km,mo.eta_minutes,pu.name,pu.latitude,pu.longitude,
               pu.location_updated_at,p.rating
        FROM match_offers mo
        JOIN providers p ON p.id=mo.provider_id
        JOIN users pu ON pu.id=p.user_id
        WHERE mo.request_id=?
        ORDER BY mo.distance_km ASC,mo.id DESC
    """,(request_id,)).fetchall()
    data["offers"]=[dict(x) for x in offers]
    quote=_quote_for_request(connection,request_id)
    data["quote"]=quote
    # Private arrival PIN is visible only to the customer who owns the request.
    if int(row["customer_id"]) != int(viewer_id):
        data["confirmation_code"] = None
    try:
        data["completion_proof_paths"] = json.loads(data.get("completion_proof_paths") or "[]")
    except Exception:
        data["completion_proof_paths"] = []
    return data

@app.route("/searching/<int:request_id>")
def searching(request_id):
    if "user_id" not in session or session.get("role")!="customer":
        return redirect(url_for("login"))
    connection=get_db_connection()
    row=connection.execute("""
        SELECT sr.*,s.name AS service_name
        FROM service_requests sr JOIN services s ON s.id=sr.service_id
        WHERE sr.id=? AND sr.customer_id=?
    """,(request_id,session["user_id"])).fetchone()
    connection.close()
    if not row:
        flash("Service request not found.")
        return redirect(url_for("customer_dashboard"))
    return render_template("searching.html", service_request=row)

@app.route("/api/matching/start/<int:request_id>", methods=["POST"])
def api_matching_start(request_id):
    # Customer-led discovery: this endpoint never creates provider offers or notifies providers.
    if "user_id" not in session or session.get("role")!="customer":
        return jsonify({"success":False,"message":"Customer login required"}),401
    connection=get_db_connection()
    row=connection.execute("SELECT * FROM service_requests WHERE id=? AND customer_id=?",(request_id,session["user_id"])).fetchone()
    if not row:
        connection.close(); return jsonify({"success":False,"message":"Request not found"}),404
    data=request.get_json(silent=True) or {}
    if row["status"] == "SCHEDULED":
        try:
            if row["scheduled_at"] and datetime.fromisoformat(str(row["scheduled_at"]).replace('Z','')) > datetime.now():
                connection.close(); return jsonify({"success":True,"scheduled":True,"count":0,"message":"This service is scheduled; provider discovery will open near the scheduled time."})
        except Exception:
            pass
        connection.execute("UPDATE service_requests SET status='SEARCHING' WHERE id=?",(request_id,)); row=connection.execute("SELECT * FROM service_requests WHERE id=?",(request_id,)).fetchone()
    try: radius=float(data.get("radius_km",row["service_radius_km"] or 10))
    except (TypeError,ValueError): radius=10
    radius=max(1,min(radius,100))
    if (row["customer_latitude"] is None or row["customer_longitude"] is None) and not (str(row["customer_pincode"] or "").isdigit() and len(str(row["customer_pincode"] or ""))==6):
        connection.close(); return jsonify({"success":False,"message":"Enter a valid 6-digit PIN code before finding providers.","location_required":True}),400
    connection.execute("UPDATE service_requests SET status='SEARCHING',service_radius_km=?,search_started_at=COALESCE(search_started_at,CURRENT_TIMESTAMP),search_deadline=NULL WHERE id=? AND customer_id=? AND provider_id IS NULL",(radius,request_id,session["user_id"]))
    quote=_quote_for_request(connection,request_id)
    if quote:
        connection.execute("UPDATE service_requests SET upfront_min=?,upfront_max=?,platform_fee=?,travel_fee=? WHERE id=?",(quote["min"],quote["max"],quote["platform_fee"],quote["travel_fee"],request_id))
    try:
        providers=_with_photo_urls(available_providers(connection,request_id,radius,100))
    except Exception as exc:
        connection.rollback(); connection.close()
        print("MATCHING START ERROR:", repr(exc))
        return jsonify({"success":False,"message":"Provider discovery could not be completed. Restart SmartServe once to apply the database migration.","error":str(exc)}),500
    _log_event(connection,request_id,session["user_id"],"PROVIDER_DISCOVERY_STARTED",f"Customer can compare {len(providers)} available providers.",{"count":len(providers),"radius_km":radius})
    connection.commit(); connection.close()
    return jsonify({"success":True,"request_id":request_id,"radius_km":radius,"count":len(providers),"providers":providers})

@app.route("/api/matching/status/<int:request_id>")
def api_matching_status(request_id):
    if "user_id" not in session:
        return jsonify({"success":False,"message":"Login required"}),401
    connection=get_db_connection()
    data=_serialize_request_status(connection,request_id,session["user_id"])
    if not data:
        connection.close(); return jsonify({"success":False,"message":"Not authorized"}),403
    connection.close()
    return jsonify({"success":True,"request":data})

@app.route("/api/nearby-providers/<int:request_id>")
def api_nearby_providers(request_id):
    if "user_id" not in session or session.get("role")!="customer":
        return jsonify({"success":False,"message":"Customer login required"}),401
    connection=get_db_connection()
    row=connection.execute("SELECT * FROM service_requests WHERE id=? AND customer_id=?",(request_id,session["user_id"])).fetchone()
    if not row:
        connection.close(); return jsonify({"success":False,"message":"Request not found"}),404
    try: radius=max(1,min(float(request.args.get("radius",row["service_radius_km"] or 10)),100))
    except (TypeError,ValueError): radius=10
    try:
        providers=_with_photo_urls(available_providers(connection,request_id,radius,100))
    except Exception as exc:
        connection.close()
        print("NEARBY PROVIDERS ERROR:", repr(exc))
        return jsonify({"success":False,"message":"Provider discovery failed. Restart SmartServe so database migrations can run.","error":str(exc)}),500
    connection.close()
    return jsonify({"success":True,"radius_km":radius,"count":len(providers),"providers":providers})

@app.route("/api/matching/diagnostic/<int:request_id>")
def api_matching_diagnostic(request_id):
    if "user_id" not in session or session.get("role") != "customer":
        return jsonify({"success":False,"message":"Customer login required"}),401
    connection=get_db_connection()
    req=connection.execute("SELECT id FROM service_requests WHERE id=? AND customer_id=?",(request_id,session["user_id"])).fetchone()
    if not req:
        connection.close(); return jsonify({"success":False,"message":"Request not found"}),404
    data=diagnose(connection,request_id)
    connection.close()
    return jsonify({"success":True,"providers":data})

@app.route("/api/matching/offer/<int:request_id>/<int:provider_id>/<action>",methods=["POST"])
def api_matching_offer(request_id,provider_id,action):
    return jsonify({"success":False,"message":"Provider offers are disabled. SmartServe now uses customer-led provider selection."}),410

@app.route("/api/service-request/<int:request_id>/cancel",methods=["POST"])
def cancel_service_request(request_id):
    if "user_id" not in session or session.get("role")!="customer":
        return jsonify({"success":False,"message":"Customer login required"}),401
    data=request.get_json(silent=True) or {}
    reason=str(data.get("reason","Customer cancelled")).strip()[:250]
    connection=get_db_connection()
    cur=connection.execute("""
        UPDATE service_requests SET status='CANCELLED',cancelled_at=CURRENT_TIMESTAMP,cancellation_reason=?
        WHERE id=? AND customer_id=? AND status IN ('PENDING','SEARCHING','OFFERED','ASSIGNED','ACCEPTED')
    """,(reason,request_id,session["user_id"]))
    if cur.rowcount:
        connection.execute("UPDATE match_offers SET status='CANCELLED',responded_at=CURRENT_TIMESTAMP WHERE request_id=? AND status='OFFERED'",(request_id,))
        _log_event(connection,request_id,session["user_id"],"CANCELLED",reason)
    connection.commit(); connection.close()
    if cur.rowcount: _emit_request_update(request_id)
    return jsonify({"success":bool(cur.rowcount),"message":"Booking cancelled." if cur.rowcount else "This booking cannot be cancelled now."})

@app.route("/api/negotiation/<int:request_id>",methods=["GET","POST"])
def negotiation(request_id):
    if "user_id" not in session:
        return jsonify({"success":False,"message":"Login required"}),401
    connection=get_db_connection()
    req=connection.execute("""
        SELECT sr.*,p.user_id AS provider_user_id
        FROM service_requests sr LEFT JOIN providers p ON p.id=sr.provider_id
        WHERE sr.id=?
    """,(request_id,)).fetchone()
    if not req:
        connection.close(); return jsonify({"success":False,"message":"Request not found"}),404
    if session["user_id"] not in {req["customer_id"],req["provider_user_id"]}:
        connection.close(); return jsonify({"success":False,"message":"Not authorized"}),403
    if request.method=="POST":
        if str(req["negotiation_status"] or "NONE").upper() == "ACCEPTED":
            connection.close(); return jsonify({"success":False,"message":"Final price is already agreed."}),400
        if req["status"] not in ("ASSIGNED","ACCEPTED","IN_PROGRESS","AWAITING_VERIFICATION","AWAITING_PAYMENT"):
            connection.close(); return jsonify({"success":False,"message":"Select a provider before negotiating."}),400
        data=request.get_json(silent=True) or {}
        try: amount=float(data.get("amount",0))
        except: amount=0
        message=str(data.get("message","")).strip()[:500]
        if amount<=0 or amount>1000000:
            connection.close(); return jsonify({"success":False,"message":"Enter a valid amount."}),400
        cur=connection.execute("""
            INSERT INTO price_negotiations(request_id,sender_user_id,proposed_amount,message,status)
            VALUES(?,?,?,?,'PENDING')
        """,(request_id,session["user_id"],round(amount,2),message))
        connection.execute("UPDATE service_requests SET negotiation_status='PENDING' WHERE id=?", (request_id,))
        _log_event(connection,request_id,session["user_id"],"PRICE_PROPOSED",f"₹{amount:,.2f}",{"amount":amount})
        connection.commit()
        if socketio: socketio.emit("price_proposal",{"request_id":request_id,"amount":amount,"message":message},to=_request_room(request_id))
    rows=connection.execute("""
        SELECT n.*,u.name AS sender_name FROM price_negotiations n JOIN users u ON u.id=n.sender_user_id
        WHERE n.request_id=? ORDER BY n.id ASC
    """,(request_id,)).fetchall()
    connection.close()
    return jsonify({"success":True,"negotiations":[dict(r) for r in rows]})

@app.route("/api/negotiation/<int:request_id>/<int:negotiation_id>/<action>",methods=["POST"])
def negotiation_action(request_id,negotiation_id,action):
    if "user_id" not in session: return jsonify({"success":False,"message":"Login required"}),401
    connection=get_db_connection()
    req=connection.execute("SELECT customer_id,p.user_id AS provider_user_id FROM service_requests sr LEFT JOIN providers p ON p.id=sr.provider_id WHERE sr.id=?",(request_id,)).fetchone()
    if not req or session["user_id"] not in {req["customer_id"],req["provider_user_id"]}:
        connection.close(); return jsonify({"success":False,"message":"Not authorized"}),403
    n=connection.execute("SELECT * FROM price_negotiations WHERE id=? AND request_id=? AND status='PENDING'",(negotiation_id,request_id)).fetchone()
    if not n:
        connection.close(); return jsonify({"success":False,"message":"Proposal unavailable"}),404
    if action not in ("accept","reject"): connection.close(); return jsonify({"success":False,"message":"Invalid action"}),400
    if n["sender_user_id"]==session["user_id"]:
        connection.close(); return jsonify({"success":False,"message":"You cannot accept your own proposal"}),400
    status="ACCEPTED" if action=="accept" else "REJECTED"
    connection.execute("UPDATE price_negotiations SET status=?,responded_at=CURRENT_TIMESTAMP WHERE id=?",(status,negotiation_id))
    if action=="accept":
        connection.execute("UPDATE service_requests SET agreed_amount=?,final_amount=?,negotiation_status='ACCEPTED' WHERE id=?",(n["proposed_amount"],n["proposed_amount"],request_id))
        connection.execute("UPDATE price_negotiations SET status='CLOSED',responded_at=CURRENT_TIMESTAMP WHERE request_id=? AND id<>? AND status='PENDING'",(request_id,negotiation_id))
    else:
        connection.execute("UPDATE service_requests SET negotiation_status='REJECTED' WHERE id=?",(request_id,))
    _log_event(connection,request_id,session["user_id"],"PRICE_"+action.upper(),f"Price proposal {status.lower()}.")
    connection.commit(); connection.close()
    if socketio: socketio.emit("price_update",{"request_id":request_id,"negotiation_id":negotiation_id,"status":status},to=_request_room(request_id))
    _emit_request_update(request_id)
    return jsonify({"success":True,"status":status})


# ---------------- FIND PROVIDERS ----------------


@app.route("/find-providers/<int:request_id>")
def find_providers(request_id):
    if "user_id" not in session or session.get("role") != "customer":
        return redirect(url_for("login"))
    connection=get_db_connection()
    service_request=connection.execute("""SELECT sr.*,s.name AS service_name FROM service_requests sr
        JOIN services s ON s.id=sr.service_id WHERE sr.id=? AND sr.customer_id=?""",(request_id,session["user_id"])).fetchone()
    if not service_request:
        connection.close(); flash("Service request not found."); return redirect(url_for("customer_dashboard"))
    radius=max(1,min(float(request.args.get("radius",service_request["service_radius_km"] or 10)),100))
    providers=_with_photo_urls(available_providers(connection,request_id,radius,100))
    connection.close()
    return render_template("providers.html",providers=providers,service_request=service_request,radius_km=radius,has_customer_location=bool(service_request["customer_latitude"] is not None and service_request["customer_longitude"] is not None))

@app.route("/api/providers/select/<int:request_id>/<int:provider_id>", methods=["POST"])
def customer_select_provider(request_id, provider_id):
    if "user_id" not in session or session.get("role") != "customer":
        return jsonify({"success":False,"message":"Customer login required"}),401
    connection=get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        req=connection.execute("SELECT sr.*,s.name AS service_name FROM service_requests sr JOIN services s ON s.id=sr.service_id WHERE sr.id=? AND sr.customer_id=?",(request_id,session["user_id"])).fetchone()
        if not req:
            connection.rollback(); return jsonify({"success":False,"message":"Request not found"}),404
        if req["provider_id"] is not None or req["status"] in ("ACCEPTED","IN_PROGRESS","AWAITING_VERIFICATION","AWAITING_PAYMENT","COMPLETED"):
            connection.rollback(); return jsonify({"success":False,"message":"A provider is already selected for this booking."}),409
        providers=_with_photo_urls(available_providers(connection,request_id,float(req["service_radius_km"] or 10),100))
        chosen=next((x for x in providers if int(x["provider_id"])==int(provider_id)),None)
        if not chosen:
            connection.rollback(); return jsonify({"success":False,"message":"That provider is no longer available. Refresh and choose another."}),409
        now=datetime.utcnow().isoformat(" ")
        cur=connection.execute("""UPDATE service_requests SET provider_id=?,status='ASSIGNED',accepted_at=NULL,search_deadline=NULL
            WHERE id=? AND customer_id=? AND provider_id IS NULL AND status IN ('PENDING','SEARCHING','OFFERED')""",(provider_id,request_id,session["user_id"]))
        if cur.rowcount!=1:
            connection.rollback(); return jsonify({"success":False,"message":"The booking changed while you were selecting a provider. Refresh and try again."}),409
        connection.execute("UPDATE match_offers SET status='CANCELLED',responded_at=CURRENT_TIMESTAMP WHERE request_id=? AND status IN ('OFFERED','ACCEPTED')",(request_id,))
        _log_event(connection,request_id,session["user_id"],"CUSTOMER_SELECTED_PROVIDER",f"Customer selected {chosen['name']}.",{"provider_id":provider_id,"trust_score":chosen["trust_score"],"distance_km":chosen["distance_km"]})
        connection.commit()
    except Exception as exc:
        connection.rollback(); return jsonify({"success":False,"message":str(exc)}),500
    finally:
        connection.close()
    if socketio:
        socketio.emit("provider_selected",{"request_id":request_id,"provider":chosen,"status":"ASSIGNED"},to=f"user:{chosen['user_id']}")
    _emit_request_update(request_id,event="booking_confirmed")
    return jsonify({"success":True,"message":f"{chosen['name']} was selected. The request has been sent to them for acceptance.","provider":chosen,"request_id":request_id,"status":"ASSIGNED"})

# ---------------- CUSTOMER CHOICE REDIRECT ----------------
@app.route("/select-provider/<int:request_id>/<int:provider_id>", methods=["POST"])
def select_provider(request_id, provider_id):
    if "user_id" not in session or session.get("role") != "customer":
        return redirect(url_for("login"))
    return redirect(url_for("find_providers",request_id=request_id,preselect=provider_id))


@app.route("/provider/profile", methods=["GET","POST"])
def provider_profile_edit():
    if "user_id" not in session or session.get("role") != "provider":
        return redirect(url_for("login"))
    connection=get_db_connection()
    provider=connection.execute("SELECT p.*,u.name,u.email,u.phone,u.profile_photo_path FROM providers p JOIN users u ON u.id=p.user_id WHERE p.user_id=?",(session["user_id"],)).fetchone()
    if not provider:
        connection.close(); flash("Provider profile not found."); return redirect(url_for("provider_dashboard"))
    available_services=connection.execute("SELECT id,name,description FROM services ORDER BY name").fetchall()
    if request.method=="POST":
        name=request.form.get("name","").strip()[:120]
        phone=request.form.get("phone","").strip()[:30]
        selected_service_ids=request.form.getlist("service_ids")
        skills=request.form.get("skills","").strip()[:500]
        bio=request.form.get("bio","").strip()[:1000]
        try: experience=max(0,int(request.form.get("experience","0") or 0))
        except ValueError: experience=0
        if name: connection.execute("UPDATE users SET name=?,phone=? WHERE id=?",(name,phone or None,session["user_id"]))
        else: connection.execute("UPDATE users SET phone=? WHERE id=?",(phone or None,session["user_id"]))
        provider_id=int(provider["id"])
        selected_rows=_sync_provider_services(connection, provider_id, selected_service_ids)
        if not selected_rows and skills:
            legacy_ids=[]
            for token in skills.replace(';', ',').replace('|', ',').split(','):
                row=connection.execute("SELECT id FROM services WHERE LOWER(name)=LOWER(?)", (token.strip(),)).fetchone()
                if row: legacy_ids.append(row["id"])
            if legacy_ids: selected_rows=_sync_provider_services(connection, provider_id, legacy_ids)
        connection.execute("UPDATE providers SET experience=?,bio=? WHERE user_id=?",(experience,bio or None,session["user_id"]))
        photo=request.files.get("profile_photo")
        if photo and photo.filename:
            try:
                new_path=_save_public_image(photo, f"provider_{session['user_id']}")
            except ValueError as exc:
                connection.rollback(); connection.close(); flash(f"Profile photo not saved: {exc}"); return redirect(url_for("provider_profile_edit"))
            if provider["profile_photo_path"]:
                _delete_media(provider["profile_photo_path"])
            connection.execute("UPDATE users SET profile_photo_path=? WHERE id=?",(new_path,session["user_id"]))
        if request.form.get("remove_photo")=="1" and provider["profile_photo_path"] and not (photo and photo.filename):
            _delete_media(provider["profile_photo_path"])
            connection.execute("UPDATE users SET profile_photo_path=NULL WHERE id=?",(session["user_id"],))
        ekyc=request.files.get("ekyc_document")
        if ekyc and ekyc.filename:
            if not allowed_document(ekyc.filename):
                connection.rollback(); connection.close(); flash("eKYC document must be a JPG, PNG, WEBP or PDF file."); return redirect(url_for("provider_profile_edit"))
            ext=ekyc.filename.rsplit('.',1)[1].lower(); filename=secure_filename(f"ekyc_{session['user_id']}_{uuid.uuid4().hex}.{ext}")
            ekyc.save(os.path.join(app.config["PRIVATE_UPLOAD_FOLDER"],filename))
            connection.execute("UPDATE providers SET ekyc_document_path=?,ekyc_status='SUBMITTED' WHERE user_id=?",(filename,session["user_id"]))
        # Refresh public trust badges after every profile update. Private KYC files are never exposed.
        refreshed=connection.execute("SELECT p.id,p.ekyc_status,p.experience,p.skills,p.bio,u.phone,u.profile_photo_path FROM providers p JOIN users u ON u.id=p.user_id WHERE p.user_id=?",(session["user_id"],)).fetchone()
        if refreshed:
            pid=refreshed["id"]
            badge_rules=[('phone_verified','Phone added',bool(refreshed['phone'])),('profile_complete','Profile complete',bool(refreshed['skills'] and refreshed['bio'] and refreshed['experience'] is not None)),('experienced','Experienced provider',int(refreshed['experience'] or 0)>=5),('identity_submitted','eKYC submitted',str(refreshed['ekyc_status'] or '').upper() in ('SUBMITTED','VERIFIED'))]
            for bk,label,ok in badge_rules:
                if ok: connection.execute("INSERT OR IGNORE INTO provider_badges(provider_id,badge_key,label) VALUES(?,?,?)",(pid,bk,label))
                else: connection.execute("DELETE FROM provider_badges WHERE provider_id=? AND badge_key=?",(pid,bk))
        connection.commit()
        fresh=connection.execute("SELECT name,profile_photo_path FROM users WHERE id=?",(session["user_id"],)).fetchone()
        if fresh:
            session["user_name"]=fresh["name"]; session["profile_photo_path"]=fresh["profile_photo_path"]
        connection.close(); flash("Profile updated successfully."); return redirect(url_for("provider_profile_edit"))
    selected_service_ids=[int(r["service_id"]) for r in connection.execute("SELECT service_id FROM provider_services WHERE provider_id=?", (provider["id"],)).fetchall()]
    badges=connection.execute("SELECT label,badge_key FROM provider_badges WHERE provider_id=? ORDER BY id",(provider["id"],)).fetchall()
    service_groups=_grouped_services(connection)
    connection.close(); return render_template("provider_profile_edit.html",provider=provider,services=available_services,service_groups=service_groups,selected_service_ids=selected_service_ids,badges=badges)

@app.route("/provider/ekyc/<int:provider_id>")
def provider_ekyc(provider_id):
    if "user_id" not in session or session.get("role")!="provider": return redirect(url_for("login"))
    connection=get_db_connection(); row=connection.execute("SELECT ekyc_document_path,user_id FROM providers WHERE id=?",(provider_id,)).fetchone(); connection.close()
    if not row or int(row["user_id"])!=int(session["user_id"]): return "Not found",404
    if not row["ekyc_document_path"]: return "No eKYC document uploaded.",404
    from flask import send_file
    path=os.path.join(app.config["PRIVATE_UPLOAD_FOLDER"],row["ekyc_document_path"])
    if not os.path.isfile(path): return "Document not found.",404
    return send_file(path,as_attachment=False)

@app.route("/provider/<int:provider_id>/reviews")
def provider_reviews(provider_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    connection=get_db_connection()
    provider=connection.execute("""
        SELECT p.id,p.rating,u.name,u.profile_photo_path
        FROM providers p JOIN users u ON u.id=p.user_id
        WHERE p.id=? AND p.approved=1
    """,(provider_id,)).fetchone()
    reviews=connection.execute("""
        SELECT r.rating,r.review,r.created_at,u.name AS customer_name,s.name AS service_name
        FROM reviews r JOIN users u ON u.id=r.customer_id
        JOIN service_requests sr ON sr.id=r.request_id
        JOIN services s ON s.id=sr.service_id
        WHERE r.provider_id=? ORDER BY r.created_at DESC
    """,(provider_id,)).fetchall()
    connection.close()
    if not provider: return "Provider not found",404
    return render_template("provider_reviews.html", provider=provider, reviews=reviews)

@app.route("/provider/<int:provider_id>")
def provider_profile(provider_id):
    if "user_id" not in session: return redirect(url_for("login"))
    connection=get_db_connection()
    provider=connection.execute("""SELECT p.*,u.name,u.email,u.phone,u.profile_photo_path,u.latitude,u.longitude,u.is_online,u.pincode,u.location_source,
        (SELECT COUNT(*) FROM reviews rv WHERE rv.provider_id=p.id) AS review_count,
        (SELECT COUNT(*) FROM service_requests sr2 WHERE sr2.provider_id=p.id AND sr2.status='COMPLETED') AS completed_jobs
        FROM providers p JOIN users u ON u.id=p.user_id WHERE p.id=? AND p.approved=1""",(provider_id,)).fetchone()
    reviews=connection.execute("SELECT r.rating,r.review,r.created_at,u.name AS customer_name FROM reviews r JOIN users u ON u.id=r.customer_id WHERE r.provider_id=? ORDER BY r.created_at DESC LIMIT 8",(provider_id,)).fetchall() if provider else []
    badges=connection.execute("SELECT label,badge_key FROM provider_badges WHERE provider_id=? ORDER BY id",(provider_id,)).fetchall() if provider else []
    portfolio=connection.execute("SELECT title,description,image_path,created_at FROM portfolio_items WHERE provider_id=? ORDER BY created_at DESC LIMIT 12",(provider_id,)).fetchall() if provider else []
    provider_services=connection.execute("SELECT s.id,s.name,s.icon,s.category FROM provider_services ps JOIN services s ON s.id=ps.service_id WHERE ps.provider_id=? ORDER BY COALESCE(s.sort_order,100),s.name",(provider_id,)).fetchall() if provider else []
    is_favorite=bool(connection.execute("SELECT 1 FROM favorite_providers WHERE customer_id=? AND provider_id=?",(session["user_id"],provider_id)).fetchone()) if provider and session.get("role")=="customer" else False
    connection.close()
    if not provider: flash("Provider not found."); return redirect(url_for("customer_dashboard") if session.get("role")!="provider" else url_for("provider_dashboard"))
    data=dict(provider); rating=min(5,max(0,float(data.get("rating") or 0))); exp=min(10,max(0,int(data.get("experience") or 0))); completed=min(50,int(data.get("completed_jobs") or 0)); completeness=sum(bool(data.get(k)) for k in ("phone","profile_photo_path","bio","skills","experience","ekyc_status"))/6
    data["trust_score"]=round((rating/5)*45+(exp/10)*15+completeness*15+(completed/50)*15+(10 if str(data.get('ekyc_status') or '').upper() in ('SUBMITTED','VERIFIED') else 0),1)
    return render_template("provider_profile.html",provider=data,reviews=reviews,badges=badges,portfolio=portfolio,provider_services=provider_services,is_favorite=is_favorite)

# ---------------- PROVIDER RESPONSE ----------------

@app.route("/provider-response/<int:request_id>/<action>",methods=["POST"])
def provider_response(request_id,action):
    if "user_id" not in session or session.get("role")!="provider":
        return redirect(url_for("login"))
    if action not in ("accept","reject"):
        flash("Invalid provider response.")
        return redirect(url_for("provider_dashboard"))
    connection=get_db_connection()
    provider=connection.execute("SELECT id FROM providers WHERE user_id=?",(session["user_id"],)).fetchone()
    if not provider:
        connection.close(); flash("Provider profile not found."); return redirect(url_for("provider_dashboard"))
    try:
        connection.execute("BEGIN IMMEDIATE")
        req=connection.execute("SELECT * FROM service_requests WHERE id=? AND provider_id=? AND status='ASSIGNED'",(request_id,provider["id"])).fetchone()
        if not req:
            connection.rollback(); connection.close(); flash("This request is no longer waiting for your response."); return redirect(url_for("provider_dashboard"))
        if action=="accept":
            confirmation_code = f"{secrets.randbelow(900000) + 100000:06d}"
            cur=connection.execute("UPDATE service_requests SET status='ACCEPTED',accepted_at=CURRENT_TIMESTAMP,confirmation_code=?,arrival_status='NOT_STARTED' WHERE id=? AND provider_id=? AND status='ASSIGNED'",(confirmation_code,request_id,provider["id"]))
            if cur.rowcount!=1:
                connection.rollback(); connection.close(); flash("Another action changed this request."); return redirect(url_for("provider_dashboard"))
            _log_event(connection,request_id,session["user_id"],"PROVIDER_ACCEPTED","Provider accepted the customer-selected service request.",{"provider_id":provider["id"]})
            connection.commit(); connection.close()
            if socketio:
                socketio.emit("provider_response",{"request_id":request_id,"status":"ACCEPTED"},to=f"user:{req['customer_id']}")
            _emit_request_update(request_id,event="provider_accepted")
            flash("Service accepted. Chat, calling and negotiation are now available.")
        else:
            cur=connection.execute("UPDATE service_requests SET provider_id=NULL,status='SEARCHING',accepted_at=NULL WHERE id=? AND provider_id=? AND status='ASSIGNED'",(request_id,provider["id"]))
            if cur.rowcount!=1:
                connection.rollback(); connection.close(); flash("Another action changed this request."); return redirect(url_for("provider_dashboard"))
            _log_event(connection,request_id,session["user_id"],"PROVIDER_REJECTED","Provider declined the customer-selected service request.",{"provider_id":provider["id"]})
            connection.commit(); connection.close()
            if socketio:
                socketio.emit("provider_response",{"request_id":request_id,"status":"REJECTED"},to=f"user:{req['customer_id']}")
            _emit_request_update(request_id,event="provider_rejected")
            flash("Request declined. The customer can choose another available professional.")
    except Exception as exc:
        connection.rollback(); connection.close(); flash(f"Unable to process response: {exc}")
    return redirect(url_for("provider_dashboard"))


# ---------------- SERVICE STATUS ----------------

@app.route(
    "/update-service-status/<int:request_id>/<new_status>",
    methods=["POST"]
)
def update_service_status(request_id, new_status):

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session["role"] != "provider":
        return redirect(url_for("login"))

    # Completion is deliberately NOT a direct status transition.
    # A provider must upload proof first; the customer then verifies it;
    # payment verification finally moves the request to COMPLETED.
    allowed_statuses = {
        "ARRIVED", "IN_PROGRESS"
    }

    if new_status not in allowed_statuses:

        flash("Invalid service status.")

        return redirect(
            url_for("provider_dashboard")
        )

    connection = get_db_connection()

    provider = connection.execute("""
        SELECT *
        FROM providers
        WHERE user_id = ?
    """, (
        session["user_id"],
    )).fetchone()

    if not provider:
        connection.close()

        flash("Provider profile not found.")

        return redirect(
            url_for("provider_dashboard")
        )

    service_request = connection.execute("""
        SELECT *
        FROM service_requests
        WHERE id = ?
        AND provider_id = ?
    """, (
        request_id,
        provider["id"]
    )).fetchone()

    if not service_request:
        connection.close()

        flash("Service request not found.")

        return redirect(
            url_for("provider_dashboard")
        )

    current_status = service_request["status"]

    # Make sure status transitions happen in order

    if new_status == "ARRIVED":
        if current_status != "ACCEPTED":
            connection.close()
            flash("Mark arrival only after accepting the service.")
            return redirect(url_for("provider_dashboard"))
        connection.execute("""
            UPDATE service_requests
            SET status='ARRIVED', arrival_status='ARRIVED', arrived_at=CURRENT_TIMESTAMP
            WHERE id=? AND provider_id=? AND status='ACCEPTED'
        """, (request_id, provider["id"]))
    elif new_status == "IN_PROGRESS":
        connection.close()
        flash("The customer confirmation code is required before starting the service.")
        return redirect(url_for("provider_dashboard"))

    connection.commit()
    connection.close()
    _emit_request_update(request_id)

    flash(
        "Service status updated to "
        + new_status
        + "."
    )

    return redirect(
        url_for("provider_dashboard")
    )

# ---------------- ARRIVAL CONFIRMATION ----------------
@app.route("/provider/confirm-arrival/<int:request_id>", methods=["POST"])
def confirm_arrival(request_id):
    if "user_id" not in session or session.get("role") != "provider":
        return redirect(url_for("login"))
    code = "".join(ch for ch in request.form.get("confirmation_code", "") if ch.isdigit())[:6]
    connection = get_db_connection()
    provider = connection.execute("SELECT id FROM providers WHERE user_id=?", (session["user_id"],)).fetchone()
    row = connection.execute("SELECT * FROM service_requests WHERE id=? AND provider_id=?", (request_id, provider["id"] if provider else -1)).fetchone()
    if not row:
        connection.close(); flash("Service request not found."); return redirect(url_for("provider_dashboard"))
    if row["status"] != "ARRIVED":
        connection.close(); flash("Mark your arrival first."); return redirect(url_for("provider_dashboard"))
    if not code or not hmac.compare_digest(str(code), str(row["confirmation_code"] or "")):
        connection.close(); flash("Incorrect confirmation code. Ask the customer for the 6-digit code."); return redirect(url_for("provider_dashboard"))
    connection.execute("""UPDATE service_requests SET status='IN_PROGRESS', arrival_status='CONFIRMED', confirmation_verified_at=CURRENT_TIMESTAMP, service_started_at=CURRENT_TIMESTAMP WHERE id=? AND provider_id=? AND status='ARRIVED'""",(request_id,provider["id"]))
    connection.commit(); connection.close()
    _emit_request_update(request_id,event="service_started")
    flash("Confirmation code verified. You may start the service.")
    return redirect(url_for("provider_dashboard"))

# ---------------- COMPLETION PROOF / CUSTOMER VERIFICATION ----------------

@app.route("/submit-completion-proof/<int:request_id>", methods=["POST"])
def submit_completion_proof(request_id):
    if "user_id" not in session or session.get("role") != "provider":
        return redirect(url_for("login"))

    files = [f for f in request.files.getlist("proof_images") if f and f.filename]
    if not files:
        flash("Upload at least one completion proof photo before finishing the service.")
        return redirect(url_for("provider_dashboard"))
    if len(files) > 3:
        flash("You can upload up to 3 proof photos.")
        return redirect(url_for("provider_dashboard"))

    # IMPORTANT: do not keep a SQLite transaction/read connection open while
    # receiving and writing image files. This was a major contributor to
    # SQLITE_BUSY/"database is locked" errors on Windows multi-user testing.
    connection = get_db_connection()
    provider = connection.execute(
        "SELECT id FROM providers WHERE user_id = ?", (session["user_id"],)
    ).fetchone()
    provider_id = int(provider["id"]) if provider else -1
    row = connection.execute(
        "SELECT id,status,provider_id FROM service_requests WHERE id = ? AND provider_id = ?",
        (request_id, provider_id)
    ).fetchone()
    connection.close()

    if not row:
        flash("Service request not found.")
        return redirect(url_for("provider_dashboard"))
    if row["status"] != "IN_PROGRESS":
        flash("Completion proof can only be submitted while the service is in progress.")
        return redirect(url_for("provider_dashboard"))

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    saved = []
    try:
        for file in files:
            if not allowed_file(file.filename):
                flash("Only JPG, JPEG, PNG and WEBP images are allowed.")
                return redirect(url_for("provider_dashboard"))
            extension = file.filename.rsplit(".", 1)[1].lower()
            filename = secure_filename(f"completion_{request_id}_{uuid.uuid4().hex}.{extension}")
            path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            file.save(path)
            saved.append("uploads/" + os.path.basename(path))
    except Exception as exc:
        for rel in saved:
            try:
                os.remove(os.path.join(app.config["UPLOAD_FOLDER"], os.path.basename(rel)))
            except OSError:
                pass
        flash(f"Could not save completion proof: {exc}")
        return redirect(url_for("provider_dashboard"))

    connection = get_db_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        cur = connection.execute("""
            UPDATE service_requests
            SET completion_proof_paths = ?, status = 'AWAITING_VERIFICATION', completed_at = CURRENT_TIMESTAMP
            WHERE id = ? AND provider_id = ? AND status = 'IN_PROGRESS'
        """, (json.dumps(saved), request_id, provider_id))
        if cur.rowcount != 1:
            connection.rollback()
            raise RuntimeError("The service changed state while the proof was being uploaded. Please refresh and try again.")
        _log_event(connection, request_id, session["user_id"], "COMPLETION_PROOF_SUBMITTED", "Provider uploaded completion proof for customer verification.", {"count": len(saved)})
        connection.commit()
    except Exception as exc:
        try:
            connection.rollback()
        except Exception:
            pass
        connection.close()
        # Remove orphaned files when the database update fails.
        for rel in saved:
            try:
                os.remove(os.path.join(app.config["UPLOAD_FOLDER"], os.path.basename(rel)))
            except OSError:
                pass
        print("COMPLETION PROOF ERROR:", repr(exc))
        flash(f"Completion proof could not be submitted: {exc}")
        return redirect(url_for("provider_dashboard"))
    finally:
        try:
            connection.close()
        except Exception:
            pass

    _emit_request_update(request_id, event="completion_proof_submitted")
    flash("Completion proof uploaded. The customer can now review the photos.")
    return redirect(url_for("provider_dashboard"))


@app.route("/service-proof/<int:request_id>/<path:filename>")
def service_proof(request_id, filename):
    """Serve completion proof only to the customer/provider on that booking."""
    if "user_id" not in session:
        return redirect(url_for("login"))
    connection = get_db_connection()
    row = connection.execute("""
        SELECT sr.customer_id, p.user_id AS provider_user_id, sr.completion_proof_paths
        FROM service_requests sr
        LEFT JOIN providers p ON p.id=sr.provider_id
        WHERE sr.id=?
    """, (request_id,)).fetchone()
    connection.close()
    if not row or session["user_id"] not in {int(row["customer_id"]), int(row["provider_user_id"] or -1)}:
        return "Not found", 404
    try:
        paths = json.loads(row["completion_proof_paths"] or "[]")
    except Exception:
        paths = []
    wanted = "uploads/" + os.path.basename(filename)
    if wanted not in paths:
        return "Not found", 404
    target = _find_media(wanted)
    if not target:
        return "Not found", 404
    return send_from_directory(os.path.dirname(target), os.path.basename(target), as_attachment=False)


@app.route("/verify-service/<int:request_id>", methods=["POST"])
def verify_service(request_id):
    if "user_id" not in session or session.get("role") != "customer":
        return redirect(url_for("login"))

    connection = get_db_connection()
    row = connection.execute("""
        SELECT * FROM service_requests
        WHERE id = ? AND customer_id = ?
    """, (request_id, session["user_id"])).fetchone()

    if not row:
        connection.close()
        flash("Service request not found.")
        return redirect(url_for("customer_dashboard"))
    if row["status"] != "AWAITING_VERIFICATION":
        connection.close()
        flash("This service is not waiting for verification.")
        return redirect(url_for("customer_dashboard"))
    if not row["completion_proof_paths"]:
        connection.close()
        flash("Provider must upload completion proof first.")
        return redirect(url_for("customer_dashboard"))

    verification_file = request.files.get("verification_image")
    verification_path = None
    if not verification_file or not verification_file.filename:
        connection.close()
        flash("Upload a verification photo before approving the completed service.")
        return redirect(url_for("customer_dashboard"))
    if verification_file and verification_file.filename:
        if not allowed_file(verification_file.filename):
            connection.close()
            flash("Verification photo must be JPG, JPEG, PNG or WEBP.")
            return redirect(url_for("customer_dashboard"))
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
        extension = verification_file.filename.rsplit(".", 1)[1].lower()
        filename = f"verification_{request_id}_{uuid.uuid4().hex}.{extension}"
        path = os.path.join(app.config["UPLOAD_FOLDER"], secure_filename(filename))
        verification_file.save(path)
        verification_path = "uploads/" + os.path.basename(path)

    connection.execute("""
        UPDATE service_requests
        SET customer_verification_path = ?, verified_at = CURRENT_TIMESTAMP,
            status = 'AWAITING_PAYMENT'
        WHERE id = ? AND customer_id = ?
    """, (verification_path, request_id, session["user_id"]))
    connection.commit()
    connection.close()
    _emit_request_update(request_id)
    flash("Service verified. Please complete the payment to close the service.")
    return redirect(url_for("customer_dashboard"))


#---------------- CUSTOMER REQUEST STATUS ----------------

@app.route("/api/customer/request-status")
def customer_request_status():

    # Make sure customer is logged in
    if "user_id" not in session:
        return jsonify({
            "success": False,
            "message": "Not logged in"
        }), 401


    customer_id = session["user_id"]


    connection = get_db_connection()

    rows = connection.execute("""
        SELECT
            sr.id,
            sr.status
        FROM service_requests sr
        WHERE sr.customer_id = ?
        ORDER BY sr.id DESC
    """, (customer_id,)).fetchall()

    connection.close()


    requests = []

    for row in rows:

        requests.append({
            "id": row["id"],
            "status": row["status"]
        })


    return jsonify({
        "success": True,
        "requests": requests
    })

# ---------------- REVIEW SERVICE ----------------

@app.route(
    "/review-service/<int:request_id>",
    methods=["GET", "POST"]
)
def review_service(request_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    if session["role"] != "customer":
        return redirect(url_for("login"))

    connection = get_db_connection()

    service_request = connection.execute("""
        SELECT
            service_requests.*,
            services.name AS service_name,
            users.name AS provider_name
        FROM service_requests

        JOIN services
            ON service_requests.service_id = services.id

        JOIN providers
            ON service_requests.provider_id = providers.id

        JOIN users
            ON providers.user_id = users.id

        WHERE service_requests.id = ?
        AND service_requests.customer_id = ?
    """, (
        request_id,
        session["user_id"]
    )).fetchone()

    if not service_request:

        connection.close()

        flash("Service request not found.")

        return redirect(
            url_for("customer_dashboard")
        )

    if service_request["status"] != "COMPLETED" or service_request["payment_status"] != "PAID":

        connection.close()

        flash(
            "You can review only after customer verification and successful payment."
        )

        return redirect(
            url_for("customer_dashboard")
        )

    # Check whether already reviewed

    existing_review = connection.execute("""
        SELECT *
        FROM reviews
        WHERE request_id = ?
    """, (
        request_id,
    )).fetchone()

    if existing_review:

        connection.close()

        flash(
            "You have already reviewed this service."
        )

        return redirect(
            url_for("customer_dashboard")
        )

    if request.method == "POST":

        rating = request.form.get("rating")
        review = request.form.get("review", "").strip()

        try:

            rating = int(rating)

        except:

            rating = 0

        if rating < 1 or rating > 5:

            connection.close()

            flash(
                "Rating must be between 1 and 5."
            )

            return redirect(
                url_for(
                    "review_service",
                    request_id=request_id
                )
            )

        provider_id = service_request["provider_id"]

        # Save review

        connection.execute("""
            INSERT INTO reviews
            (
                request_id,
                customer_id,
                provider_id,
                rating,
                review
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            request_id,
            session["user_id"],
            provider_id,
            rating,
            review
        ))

        # Recalculate provider rating

        rating_data = connection.execute("""
            SELECT
                AVG(rating) AS average_rating
            FROM reviews
            WHERE provider_id = ?
        """, (
            provider_id,
        )).fetchone()

        new_rating = round(
            rating_data["average_rating"],
            2
        )

        # Update provider rating

        connection.execute("""
            UPDATE providers
            SET rating = ?
            WHERE id = ?
        """, (
            new_rating,
            provider_id
        ))

        connection.commit()

        connection.close()

        flash(
            "Thank you! Your review has been submitted."
        )

        return redirect(
            url_for("customer_dashboard")
        )

    connection.close()

    return render_template(
        "review.html",
        service_request=service_request
    )




# ---------------- PIN-CODE LOCATION API ----------------
@app.route("/api/location/pincode", methods=["POST"])
def update_location_from_pincode():
    if "user_id" not in session:
        return jsonify({"success": False, "message": "Please login first"}), 401
    data = request.get_json(silent=True) or {}
    pincode = "".join(ch for ch in str(data.get("pincode", "")) if ch.isdigit())
    if len(pincode) != 6:
        return jsonify({"success": False, "message": "Enter a valid 6-digit Indian PIN code."}), 400
    try:
        loc = _lookup_pincode_location(pincode)
    except ValueError:
        loc = {"latitude": None, "longitude": None, "label": f"PIN {pincode} service area", "pincode": pincode}
    connection = get_db_connection()
    connection.execute("""
        UPDATE users SET latitude=?, longitude=?, location_accuracy=1000,
            location_updated_at=CURRENT_TIMESTAMP, pincode=?, location_source='PINCODE'
        WHERE id=?
    """, (loc["latitude"], loc["longitude"], pincode, session["user_id"]))
    connection.commit(); connection.close()
    return jsonify({"success": True, "pincode": pincode, **loc, "source": "PINCODE"})

# ---------------- REAL-TIME PRESENCE ----------------
@app.route("/api/presence", methods=["POST"])
def update_presence():
    if "user_id" not in session:
        return jsonify({"success": False, "message": "Please login first"}), 401
    data = request.get_json(silent=True) or {}
    online = bool(data.get("online", True))
    connection = get_db_connection()
    connection.execute("UPDATE users SET is_online = ?, last_seen_at = CURRENT_TIMESTAMP WHERE id = ?", (1 if online else 0, session["user_id"]))
    connection.commit(); connection.close()
    if socketio:
        socketio.emit("presence_update", {"user_id": session["user_id"], "is_online": online})
    return jsonify({"success": True, "is_online": online})


# ---------------- SECURE CALL HANDOFF ----------------
@app.route("/api/call/<int:request_id>")
def call_details(request_id):
    if "user_id" not in session:
        return jsonify(success=False, message="Login required"), 401
    conn=get_db_connection()
    row=conn.execute("""
        SELECT sr.customer_id, p.user_id AS provider_user_id,
               cu.name AS customer_name, cu.phone AS customer_phone,
               pu.name AS provider_name, pu.phone AS provider_phone,
               sr.status
        FROM service_requests sr
        JOIN users cu ON cu.id=sr.customer_id
        LEFT JOIN providers p ON p.id=sr.provider_id
        LEFT JOIN users pu ON pu.id=p.user_id
        WHERE sr.id=?
    """, (request_id,)).fetchone()
    conn.close()
    if not row or session["user_id"] not in {int(row["customer_id"]), int(row["provider_user_id"] or -1)}:
        return jsonify(success=False,message="You are not part of this service"),403
    if row["status"] not in ("ASSIGNED","ACCEPTED","ARRIVED","IN_PROGRESS","AWAITING_VERIFICATION","AWAITING_PAYMENT","COMPLETED"):
        return jsonify(success=False,message="Calling becomes available after the provider is selected."),409
    if int(session["user_id"]) == int(row["customer_id"]):
        name, phone = row["provider_name"] or "Provider", row["provider_phone"]
    else:
        name, phone = row["customer_name"] or "Customer", row["customer_phone"]
    if not phone:
        return jsonify(success=False,message=f"{name} has not added a mobile number yet."),409
    return jsonify(success=True,name=name,phone=phone,tel="tel:"+str(phone).strip())


# ---------------- REAL-TIME LOCATION ----------------

TRACKING_ACTIVE_STATUSES = ("ASSIGNED", "ACCEPTED", "ARRIVED", "IN_PROGRESS", "AWAITING_VERIFICATION", "AWAITING_PAYMENT")
LIVE_LOCATION_SECONDS = 25        # GPS newer than this is "live"
STALE_LOCATION_SECONDS = 120      # older than this is treated as offline
OSRM_BASE_URL = os.getenv("OSRM_BASE_URL", "https://router.project-osrm.org").rstrip("/")
_route_cache = {}
_route_cache_lock = threading.Lock()


def _parse_db_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "").replace(" ", "T"))
    except Exception:
        return None


def _location_age_seconds(updated_at):
    dt = _parse_db_time(updated_at)
    if not dt:
        return None
    return max(0.0, (datetime.utcnow() - dt).total_seconds())


def _active_requests_for_user(connection, user_id):
    return connection.execute("""
        SELECT sr.id, sr.customer_id, p.user_id AS provider_user_id
        FROM service_requests sr
        LEFT JOIN providers p ON p.id = sr.provider_id
        WHERE (sr.customer_id = ? OR p.user_id = ?)
          AND sr.status IN ('ASSIGNED','ACCEPTED','ARRIVED','IN_PROGRESS','AWAITING_VERIFICATION','AWAITING_PAYMENT')
    """, (user_id, user_id)).fetchall()


def _store_location(user_id, latitude, longitude, accuracy=None, heading=None, speed=None):
    """Persist a browser GPS fix and fan it out to every active service room the user belongs to."""
    connection = get_db_connection()
    try:
        active_rows = _active_requests_for_user(connection, user_id)
        current = connection.execute("SELECT location_source, is_online FROM users WHERE id=?", (user_id,)).fetchone()
        pin_mode = bool(current and str(current["location_source"] or "").upper() == "PINCODE")
        if pin_mode and not active_rows:
            # The user deliberately chose a PIN-code service area for discovery. Keep it until a
            # service becomes active (when precise GPS is required for tracking).
            connection.execute("UPDATE users SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))
            connection.commit()
            return []
        connection.execute("""
            UPDATE users
            SET latitude = ?, longitude = ?, location_accuracy = ?, location_heading = ?, location_speed = ?,
                location_updated_at = CURRENT_TIMESTAMP, location_source = 'GPS',
                last_seen_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (latitude, longitude, accuracy, heading, speed, user_id))
        for active in active_rows:
            role = "customer" if int(active["customer_id"]) == int(user_id) else "provider"
            connection.execute("""
                INSERT INTO location_updates(request_id,user_id,role,latitude,longitude,accuracy,heading,speed)
                VALUES(?,?,?,?,?,?,?,?)
            """, (active["id"], user_id, role, latitude, longitude, accuracy, heading, speed))
        connection.commit()
        # Keep the history table small: only the last 200 fixes per request are needed.
        for active in active_rows:
            connection.execute("""
                DELETE FROM location_updates WHERE request_id=? AND id NOT IN (
                    SELECT id FROM location_updates WHERE request_id=? ORDER BY id DESC LIMIT 200)
            """, (active["id"], active["id"]))
        connection.commit()
    finally:
        connection.close()
    if socketio:
        updated_at = datetime.utcnow().isoformat(" ", "seconds")
        for active in active_rows:
            role = "customer" if int(active["customer_id"]) == int(user_id) else "provider"
            socketio.emit("location_update", {
                "request_id": int(active["id"]), "user_id": int(user_id), "role": role,
                "latitude": latitude, "longitude": longitude, "accuracy": accuracy,
                "heading": heading, "speed": speed, "updated_at": updated_at, "live": True,
            }, to=_request_room(active["id"]))
    return [int(a["id"]) for a in active_rows]


def _parse_location_payload(data):
    data = data or {}
    latitude = float(data.get("latitude"))
    longitude = float(data.get("longitude"))
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Coordinates out of range")
    if latitude == 0 and longitude == 0:
        raise ValueError("Invalid coordinates")

    def _opt(name, lo, hi):
        value = data.get(name)
        if value in (None, "", "null"):
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        if value != value or value < lo or value > hi:  # NaN or out of range
            return None
        return value
    return latitude, longitude, _opt("accuracy", 0, 100000), _opt("heading", 0, 360), _opt("speed", 0, 200)


@app.route("/api/location/update", methods=["POST"])
def update_location():
    if "user_id" not in session:
        return jsonify({"success": False, "message": "Please sign in first"}), 401
    try:
        latitude, longitude, accuracy, heading, speed = _parse_location_payload(request.get_json(silent=True))
    except (TypeError, ValueError) as exc:
        return jsonify({"success": False, "message": str(exc) or "Invalid coordinates"}), 400
    active_ids = _store_location(session["user_id"], latitude, longitude, accuracy, heading, speed)
    return jsonify({
        "success": True,
        "latitude": latitude,
        "longitude": longitude,
        "accuracy": accuracy,
        "active_requests": active_ids,
        "updated_at": datetime.utcnow().isoformat(" ", "seconds"),
    })


if socketio:
    @socketio.on("share_location")
    def socket_share_location(data):
        """Low-latency GPS ingestion over the existing Socket.IO connection."""
        if not session.get("user_id"):
            emit("socket_error", {"message": "Login required"})
            return
        try:
            latitude, longitude, accuracy, heading, speed = _parse_location_payload(data)
        except (TypeError, ValueError):
            emit("socket_error", {"message": "Invalid coordinates"})
            return
        try:
            active_ids = _store_location(session["user_id"], latitude, longitude, accuracy, heading, speed)
            emit("location_ack", {"ok": True, "active_requests": active_ids})
        except Exception as exc:
            print("SOCKET LOCATION ERROR:", repr(exc))
            emit("location_ack", {"ok": False})


def _route_between(origin, destination):
    """Driving route via OSRM (cached ~20s). Returns None if routing is unavailable.

    Coordinates are (lat, lon) tuples. The cache key is rounded to ~11 m so a
    stationary device does not create new routing requests every poll.
    """
    if not origin or not destination:
        return None
    key = (round(origin[0], 4), round(origin[1], 4), round(destination[0], 4), round(destination[1], 4))
    now = time.monotonic()
    with _route_cache_lock:
        cached = _route_cache.get(key)
        # Successful routes are reused for 20 s; failures only for 5 s so the road route
        # returns as soon as the routing service is reachable again.
        if cached and now - cached[0] < (20 if cached[1] else 5):
            return cached[1]
    result = None
    try:
        url = (f"{OSRM_BASE_URL}/route/v1/driving/{origin[1]},{origin[0]};{destination[1]},{destination[0]}"
               "?overview=full&geometries=geojson&steps=false&alternatives=false")
        req = urllib.request.Request(url, headers={"User-Agent": "SmartServe/7.3 live tracking"})
        with urllib.request.urlopen(req, timeout=6) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("code") == "Ok" and payload.get("routes"):
            route = payload["routes"][0]
            coords = [[pt[1], pt[0]] for pt in (route.get("geometry") or {}).get("coordinates", [])]
            result = {
                "distance_km": round(float(route["distance"]) / 1000.0, 2),
                "duration_minutes": max(1, int(round(float(route["duration"]) / 60.0))),
                "geometry": coords,
                "source": "osrm",
            }
    except Exception as exc:
        print("OSRM ROUTE WARNING:", repr(exc))
    with _route_cache_lock:
        if len(_route_cache) > 500:
            _route_cache.clear()
        _route_cache[key] = (now, result)
    return result


def _describe_location(latitude, longitude, updated_at, source, accuracy=None, heading=None, speed=None):
    """Classify a stored location as live / stale / offline / approximate without inventing data."""
    base = {"latitude": None, "longitude": None, "accuracy": accuracy, "heading": heading, "speed": speed,
            "updated_at": updated_at, "age_seconds": None, "source": (source or "").upper() or None,
            "live": False, "stale": False, "approximate": False, "state": "offline"}
    if latitude is None or longitude is None:
        base["state"] = "unknown"
        return base
    source_upper = str(source or "").upper()
    age = _location_age_seconds(updated_at)
    base["age_seconds"] = None if age is None else int(age)
    if source_upper == "PINCODE":
        # A PIN-code location is a deliberately approximate service-area point; it never "moves".
        base.update({"latitude": float(latitude), "longitude": float(longitude), "approximate": True, "state": "approximate"})
        return base
    if age is None or age > STALE_LOCATION_SECONDS:
        base["state"] = "offline"
        return base
    base.update({"latitude": float(latitude), "longitude": float(longitude)})
    if age <= LIVE_LOCATION_SECONDS:
        base.update({"live": True, "state": "live"})
    else:
        base.update({"stale": True, "state": "stale"})
    return base


def _tracking_snapshot(connection, request_id, viewer_id):
    row = connection.execute("""
        SELECT sr.id, sr.customer_id, sr.provider_id, sr.status, sr.arrival_status,
               sr.customer_latitude AS request_latitude, sr.customer_longitude AS request_longitude,
               sr.customer_pincode, sr.address_text,
               cu.name AS customer_name, cu.latitude AS customer_latitude, cu.longitude AS customer_longitude,
               cu.location_updated_at AS customer_location_updated_at, cu.location_source AS customer_location_source,
               cu.location_accuracy AS customer_accuracy, cu.location_heading AS customer_heading, cu.location_speed AS customer_speed,
               cu.profile_photo_path AS customer_photo,
               pu.id AS provider_user_id, pu.name AS provider_name, pu.latitude AS provider_latitude, pu.longitude AS provider_longitude,
               pu.location_updated_at AS provider_location_updated_at, pu.location_source AS provider_location_source,
               pu.location_accuracy AS provider_accuracy, pu.location_heading AS provider_heading, pu.location_speed AS provider_speed,
               pu.profile_photo_path AS provider_photo, pu.phone AS provider_phone, cu.phone AS customer_phone
        FROM service_requests sr
        JOIN users cu ON cu.id = sr.customer_id
        LEFT JOIN providers p ON p.id = sr.provider_id
        LEFT JOIN users pu ON pu.id = p.user_id
        WHERE sr.id = ?
    """, (request_id,)).fetchone()
    if not row:
        return None, ("Request not found", 404)
    is_customer = int(row["customer_id"]) == int(viewer_id)
    is_provider = row["provider_user_id"] is not None and int(row["provider_user_id"]) == int(viewer_id)
    if not (is_customer or is_provider):
        return None, ("You are not part of this service", 403)

    customer = _describe_location(row["customer_latitude"], row["customer_longitude"], row["customer_location_updated_at"],
                                  row["customer_location_source"], row["customer_accuracy"], row["customer_heading"], row["customer_speed"])
    # If the customer's device is offline, fall back to the approximate PIN/service address stored on the request.
    if customer["state"] in ("offline", "unknown") and row["request_latitude"] is not None and row["request_longitude"] is not None:
        customer = _describe_location(row["request_latitude"], row["request_longitude"], None, "PINCODE")
    provider = _describe_location(row["provider_latitude"], row["provider_longitude"], row["provider_location_updated_at"],
                                  row["provider_location_source"], row["provider_accuracy"], row["provider_heading"], row["provider_speed"]) \
        if row["provider_id"] else _describe_location(None, None, None, None)
    customer["name"] = row["customer_name"]; customer["photo_url"] = media_url(row["customer_photo"])
    provider["name"] = row["provider_name"]; provider["photo_url"] = media_url(row["provider_photo"])

    tracking_active = row["status"] in TRACKING_ACTIVE_STATUSES
    distance_km = None; route = None; eta_minutes = None; eta_source = None
    if tracking_active and customer["latitude"] is not None and provider["latitude"] is not None:
        origin = (provider["latitude"], provider["longitude"]); destination = (customer["latitude"], customer["longitude"])
        straight = haversine(origin[0], origin[1], destination[0], destination[1])
        distance_km = round(straight, 2)
        if row["status"] in ("ASSIGNED", "ACCEPTED"):
            route = _route_between(origin, destination)
            if route:
                distance_km = route["distance_km"]; eta_minutes = route["duration_minutes"]; eta_source = "road"
            elif straight is not None:
                # Routing service unreachable: derive an approximate ETA from the real straight-line
                # distance (x1.3 typical road detour factor at 25 km/h urban average). Clearly labelled.
                eta_minutes = max(1, int(round((straight * 1.3) / 25.0 * 60)))
                eta_source = "estimate"
    return {
        "success": True,
        "request_id": int(row["id"]),
        "status": row["status"],
        "arrival_status": row["arrival_status"],
        "tracking_active": tracking_active,
        "viewer_role": "customer" if is_customer else "provider",
        "customer": customer,
        "provider": provider,
        "distance_km": distance_km,
        "eta_minutes": eta_minutes,
        "eta_source": eta_source,
        "route": route["geometry"] if route else None,
        "route_source": route["source"] if route else None,
        "arrived_nearby": bool(distance_km is not None and distance_km <= 0.2),
        "server_time": datetime.utcnow().isoformat(" ", "seconds"),
    }, None


@app.route("/api/request-location/<int:request_id>")
def request_location(request_id):
    if "user_id" not in session:
        return jsonify({"success": False, "message": "Please sign in first"}), 401
    connection = get_db_connection()
    try:
        data, error = _tracking_snapshot(connection, request_id, session["user_id"])
    finally:
        connection.close()
    if error:
        return jsonify({"success": False, "message": error[0]}), error[1]
    return jsonify(data)


@app.route("/api/service/<int:request_id>/tracking")
def service_tracking(request_id):
    """Alias of /api/request-location for the live-service page."""
    return request_location(request_id)


# ---------------- AGREED PRICE ----------------

@app.route("/set-service-price/<int:request_id>", methods=["POST"])
def set_service_price(request_id):
    if "user_id" not in session or session.get("role") != "provider":
        return redirect(url_for("login"))

    try:
        amount = float(request.form.get("amount", "0"))
    except ValueError:
        amount = 0

    if amount <= 0 or amount > 1000000:
        flash("Enter a valid service amount.")
        return redirect(url_for("provider_dashboard"))

    connection = get_db_connection()
    provider = connection.execute(
        "SELECT id FROM providers WHERE user_id = ?",
        (session["user_id"],)
    ).fetchone()

    if not provider:
        connection.close()
        flash("Provider profile not found.")
        return redirect(url_for("provider_dashboard"))

    result = connection.execute("""
        UPDATE service_requests
        SET agreed_amount = ?
        WHERE id = ?
        AND provider_id = ?
        AND status IN ('ASSIGNED', 'ACCEPTED', 'IN_PROGRESS')
    """, (round(amount, 2), request_id, provider["id"]))
    connection.commit()
    connection.close()

    if result.rowcount != 1:
        flash("Unable to set the service amount.")
    else:
        flash(f"Agreed service amount set to ₹{amount:,.2f}.")
    return redirect(url_for("provider_dashboard"))


# ---------------- RAZORPAY PAYMENTS ----------------

@app.route("/api/payment/order/<int:request_id>", methods=["POST"])
def create_payment_order(request_id):
    if "user_id" not in session or session.get("role") != "customer":
        return jsonify({"success": False, "message": "Customer login required"}), 401

    if not razorpay_client:
        return jsonify({
            "success": False,
            "message": "Razorpay is not configured. Add RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET to .env."
        }), 503

    connection = get_db_connection()
    row = connection.execute("""
        SELECT sr.*, s.name AS service_name, u.name AS customer_name, u.email AS customer_email
        FROM service_requests sr
        JOIN services s ON s.id = sr.service_id
        JOIN users u ON u.id = sr.customer_id
        WHERE sr.id = ? AND sr.customer_id = ?
    """, (request_id, session["user_id"])).fetchone()

    if not row:
        connection.close()
        return jsonify({"success": False, "message": "Service request not found"}), 404

    if not row["provider_id"] or row["status"] != "AWAITING_PAYMENT":
        connection.close()
        return jsonify({"success": False, "message": "Payment becomes available after the customer verifies the provider's completion proof."}), 400

    if row["payment_status"] == "PAID":
        connection.close()
        return jsonify({"success": False, "message": "This service is already paid."}), 400

    amount = row["agreed_amount"]
    if amount is None or float(amount) <= 0:
        connection.close()
        return jsonify({"success": False, "message": "Provider has not set the agreed service amount yet."}), 400

    amount_paise = int(round(float(amount) * 100))
    try:
        order = razorpay_client.order.create(data={
            "amount": amount_paise,
            "currency": "INR",
            "receipt": f"smartserve_{request_id}",
            "notes": {"request_id": str(request_id)}
        })
        connection.execute("""
            UPDATE service_requests
            SET razorpay_order_id = ?, payment_status = 'PENDING'
            WHERE id = ?
        """, (order["id"], request_id))
        connection.commit()
    except Exception as exc:
        connection.close()
        app.logger.error("Razorpay order creation failed for request %s: %r", request_id, exc)
        detail = str(getattr(exc, "args", [""])[0] or "")[:160]
        hint = " Check RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET (use the matching test-mode keys) and outbound access to api.razorpay.com." if "uthentication" in detail or "401" in detail else ""
        return jsonify({"success": False, "message": "Could not create the payment order." + (f" Razorpay said: {detail}." if detail else "") + hint}), 502

    connection.close()
    return jsonify({
        "success": True,
        "key_id": RAZORPAY_KEY_ID,
        "order_id": order["id"],
        "amount": amount_paise,
        "currency": "INR",
        "name": "Smart Serve",
        "description": f"{row['service_name']} - Service Request #{request_id}",
        "prefill": {"name": row["customer_name"], "email": row["customer_email"]}
    })


@app.route("/api/payment/verify/<int:request_id>", methods=["POST"])
def verify_payment(request_id):
    if "user_id" not in session or session.get("role") != "customer":
        return jsonify({"success": False, "message": "Customer login required"}), 401

    data = request.get_json(silent=True) or {}
    order_id = str(data.get("razorpay_order_id", ""))
    payment_id = str(data.get("razorpay_payment_id", ""))
    signature = str(data.get("razorpay_signature", ""))

    connection = get_db_connection()
    row = connection.execute("""
        SELECT id, razorpay_order_id, payment_status
        FROM service_requests
        WHERE id = ? AND customer_id = ?
    """, (request_id, session["user_id"])).fetchone()

    if not row:
        connection.close()
        return jsonify({"success": False, "message": "Service request not found"}), 404

    if not order_id or not payment_id or not signature or order_id != row["razorpay_order_id"]:
        connection.close()
        return jsonify({"success": False, "message": "Invalid payment details"}), 400

    if not RAZORPAY_KEY_SECRET:
        connection.close()
        return jsonify({"success": False, "message": "Razorpay secret is not configured"}), 503

    payload = f"{order_id}|{payment_id}".encode("utf-8")
    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        connection.close()
        return jsonify({"success": False, "message": "Payment signature verification failed"}), 400

    if row["payment_status"] == "PAID":
        connection.close()
        return jsonify({"success": True, "message": "Payment already verified."})

    # Payment is the final completion gate.
    cur=connection.execute("""
        UPDATE service_requests
        SET payment_status = 'PAID', razorpay_payment_id = ?, paid_at = CURRENT_TIMESTAMP,
            status = 'COMPLETED', completed_at = CURRENT_TIMESTAMP
        WHERE id = ? AND customer_id = ? AND status = 'AWAITING_PAYMENT'
    """, (payment_id, request_id, session["user_id"]))
    if cur.rowcount != 1:
        connection.rollback(); connection.close()
        return jsonify({"success": False, "message": "Booking is no longer awaiting payment."}), 409
    paid_row=connection.execute("SELECT provider_id,agreed_amount,platform_fee FROM service_requests WHERE id=?",(request_id,)).fetchone()
    if paid_row and paid_row['provider_id']:
        gross=float(paid_row['agreed_amount'] or 0); fee=float(paid_row['platform_fee'] or 0); net=max(0,gross-fee)
        connection.execute("INSERT INTO provider_earnings(provider_id,request_id,gross_amount,platform_fee,net_amount,payout_status) VALUES(?,?,?,?,?,'PENDING')",(paid_row['provider_id'],request_id,gross,fee,net))
        # Default 7-day workmanship warranty. Providers can later configure category-specific terms.
        connection.execute("INSERT OR REPLACE INTO service_warranties(request_id,provider_id,warranty_days,warranty_until,terms) VALUES(?,?,7,datetime('now','+7 day'),'Workmanship warranty for the completed service; excludes new damage, misuse and unrelated faults.')",(request_id,paid_row['provider_id']))
        connection.execute("UPDATE service_requests SET warranty_days=7,warranty_until=datetime('now','+7 day') WHERE id=?",(request_id,))
        _award_provider_metrics(connection, int(paid_row['provider_id'])) if '_award_provider_metrics' in globals() else None
    connection.commit()
    connection.close()
    _emit_request_update(request_id)

    return jsonify({"success": True, "status":"COMPLETED", "message": "Payment verified successfully."})


@app.route("/api/chat/<int:request_id>", methods=["GET", "POST"])
def chat_messages(request_id):

    if "user_id" not in session:

        return jsonify({
            "success": False,
            "message": "Please login first"
        }), 401


    current_user_id = session["user_id"]


    connection = get_db_connection()


    # -------------------------------------------------
    # Find the request and provider
    # -------------------------------------------------

    service_request = connection.execute("""
        SELECT
            sr.id,
            sr.customer_id,
            sr.provider_id,
            sr.status,
            u.name AS provider_name

        FROM service_requests sr

        LEFT JOIN providers p
            ON p.id = sr.provider_id

        LEFT JOIN users u
            ON u.id = p.user_id

        WHERE sr.id = ?
    """, (request_id,)).fetchone()


    if not service_request:

        connection.close()

        return jsonify({
            "success": False,
            "message": "Service request not found"
        }), 404


    # -------------------------------------------------
    # Check whether current user belongs to this request
    # -------------------------------------------------

    is_customer = (
        current_user_id ==
        service_request["customer_id"]
    )


    provider_user_id = None


    if service_request["provider_id"]:

        provider = connection.execute("""
            SELECT user_id
            FROM providers
            WHERE id = ?
        """, (
            service_request["provider_id"],
        )).fetchone()


        if provider:

            provider_user_id = provider["user_id"]


    is_provider = (
        current_user_id ==
        provider_user_id
    )


    if not is_customer and not is_provider:

        connection.close()

        return jsonify({
            "success": False,
            "message": "You are not part of this service request"
        }), 403


    # -------------------------------------------------
    # Only allow chat after provider accepts
    # -------------------------------------------------

    allowed_statuses = (
        "ASSIGNED", "ACCEPTED", "IN_PROGRESS", "AWAITING_VERIFICATION", "AWAITING_PAYMENT", "COMPLETED"
    )


    if service_request["status"] not in allowed_statuses:

        connection.close()

        return jsonify({
            "success": False,
            "message":
                "Chat will be available after you select a provider"
        }), 403


    # =================================================
    # SEND MESSAGE
    # =================================================

    if request.method == "POST":

        data = request.get_json(
            silent=True
        ) or {}


        message = str(
            data.get("message", "")
        ).strip()


        if not message:

            connection.close()

            return jsonify({
                "success": False,
                "message": "Message cannot be empty"
            }), 400


        if len(message) > 1000:

            connection.close()

            return jsonify({
                "success": False,
                "message":
                    "Message cannot exceed 1000 characters"
            }), 400


        connection.execute("""
            INSERT INTO messages (
                request_id,
                sender_id,
                message
            )
            VALUES (?, ?, ?)
        """, (
            request_id,
            current_user_id,
            message
        ))


        connection.commit()
        if socketio:
            sender = connection.execute("SELECT name FROM users WHERE id = ?", (current_user_id,)).fetchone()
            socketio.emit("chat_message", {
                "request_id": request_id,
                "sender_id": current_user_id,
                "sender_name": sender["name"] if sender else "User",
                "message": message
            }, to=_request_room(request_id))


    # =================================================
    # GET MESSAGES
    # =================================================

    rows = connection.execute("""
        SELECT
            m.id,
            m.sender_id,
            m.message,
            m.created_at,
            u.name AS sender_name

        FROM messages m

        JOIN users u
            ON u.id = m.sender_id

        WHERE m.request_id = ?

        ORDER BY m.id ASC
    """, (
        request_id,
    )).fetchall()


    messages = []


    for row in rows:

        messages.append({

            "id":
                row["id"],

            "sender_id":
                row["sender_id"],

            "sender_name":
                row["sender_name"],

            "message":
                row["message"],

            "created_at":
                row["created_at"],

            "is_mine":
                row["sender_id"] ==
                current_user_id

        })


    other_user_id = provider_user_id
    if is_provider:
        customer_row = connection.execute("SELECT name,phone FROM users WHERE id=?",(service_request["customer_id"],)).fetchone()
        other_name = customer_row["name"] if customer_row else "Customer"
        other_phone = customer_row["phone"] if customer_row else None
    else:
        provider_row = connection.execute("SELECT name,phone FROM users WHERE id=(SELECT user_id FROM providers WHERE id=?)",(service_request["provider_id"],)).fetchone() if service_request["provider_id"] else None
        other_name = provider_row["name"] if provider_row else "Service Provider"
        other_phone = provider_row["phone"] if provider_row else None
    connection.close()

    return jsonify({
        "success": True,
        "other_party_name": other_name,
        "other_party_phone": other_phone,
        "provider_name": service_request["provider_name"] or "Service Provider",
        "messages": messages
    })




@app.route('/admin/kyc/<int:provider_id>/verify', methods=['POST'])
def admin_verify_kyc(provider_id):
    if not _require_role('admin'): return redirect(url_for('login'))
    c=get_db_connection(); row=c.execute('SELECT id,ekyc_document_path FROM providers WHERE id=?',(provider_id,)).fetchone()
    if not row or not row['ekyc_document_path']: c.close(); flash('No eKYC document submitted.'); return redirect(url_for('admin_operations'))
    c.execute("UPDATE providers SET ekyc_status='VERIFIED' WHERE id=?",(provider_id,)); c.execute("INSERT OR IGNORE INTO provider_badges(provider_id,badge_key,label) VALUES(?, 'identity_verified','Identity verified')",(provider_id,)); c.commit(); c.close(); flash('Provider identity marked verified.'); return redirect(url_for('admin_operations'))

@app.route('/admin/dispute/<int:dispute_id>/<action>', methods=['POST'])
def admin_dispute_action(dispute_id,action):
    if not _require_role('admin'): return redirect(url_for('login'))
    status={'resolve':'RESOLVED','reject':'REJECTED'}.get(action)
    if not status: return redirect(url_for('admin_operations'))
    resolution=request.form.get('resolution','').strip()[:500] or ('Operations team '+status.lower()+' this case.')
    c=get_db_connection(); c.execute('UPDATE disputes SET status=?,updated_at=CURRENT_TIMESTAMP,resolved_at=CURRENT_TIMESTAMP,resolution=? WHERE id=?',(status,resolution,dispute_id))
    row=c.execute('SELECT request_id,opened_by FROM disputes WHERE id=?',(dispute_id,)).fetchone()
    if row:
        c.execute("UPDATE admin_alerts SET status='RESOLVED',resolved_at=CURRENT_TIMESTAMP WHERE request_id=? AND user_id=? AND title='New dispute' AND status='OPEN'",(row['request_id'],row['opened_by']))
        _log_event(c,int(row['request_id']),session['user_id'],'DISPUTE_'+status,resolution,{'dispute_id':dispute_id})
    c.commit(); c.close()
    if socketio and row: socketio.emit('dispute_update',{'dispute_id':dispute_id,'status':status,'resolution':resolution},to=f"user:{row['opened_by']}")
    flash(f'Dispute #{dispute_id} marked {status.lower()}.'); return redirect(url_for('admin_operations'))

# =========================================================
# V7 LIVE SERVICE + TRUST / MARKETPLACE FEATURES
# =========================================================
@app.route('/features')
def marketplace_features():
    if 'user_id' not in session: return redirect(url_for('login'))
    hub={}
    if session.get('role')=='provider':
        conn=get_db_connection(); pid=_provider_id_for_user(conn,session['user_id'])
        if pid:
            p=conn.execute("SELECT p.rating,p.experience,p.bio,p.ekyc_status,u.phone,u.profile_photo_path FROM providers p JOIN users u ON u.id=p.user_id WHERE p.id=?",(pid,)).fetchone()
            hub={
                'portfolio_count':conn.execute("SELECT COUNT(*) c FROM portfolio_items WHERE provider_id=?",(pid,)).fetchone()['c'],
                'review_count':conn.execute("SELECT COUNT(*) c FROM reviews WHERE provider_id=?",(pid,)).fetchone()['c'],
                'rating':float(p['rating'] or 0) if p else 0,
                'net_earnings':float(conn.execute("SELECT COALESCE(SUM(net_amount),0) n FROM provider_earnings WHERE provider_id=?",(pid,)).fetchone()['n']),
                'service_count':conn.execute("SELECT COUNT(*) c FROM provider_services WHERE provider_id=?",(pid,)).fetchone()['c'],
                'ekyc_status':(p['ekyc_status'] if p else 'NOT_SUBMITTED') or 'NOT_SUBMITTED',
                'profile_completeness':round(100*sum(1 for k in ('phone','profile_photo_path','bio','experience') if p and p[k] not in (None,'',0))/4) if p else 0,
            }
        conn.close()
    return render_template('marketplace_features.html',hub=hub)

@app.route('/api/service/<int:request_id>/arrival-check')
def arrival_check(request_id):
    if 'user_id' not in session: return jsonify(success=False,message='Login required'),401
    c=get_db_connection()
    try:
        data,error=_tracking_snapshot(c,request_id,session['user_id'])
        if error: return jsonify(success=False,message=error[0]),error[1]
        if data['distance_km'] is None:
            return jsonify(success=True,nearby=False,distance_km=None,message='Waiting for both live locations.')
        nearby=bool(data['arrived_nearby'])
        if nearby and data['status']=='ACCEPTED':
            c.execute("UPDATE service_requests SET route_status='NEAR_CUSTOMER' WHERE id=?",(request_id,)); c.commit()
        return jsonify(success=True,nearby=nearby,distance_km=data['distance_km'],eta_minutes=data['eta_minutes'],message='Provider is within 200 metres.' if nearby else 'Provider is on the way.')
    finally:
        c.close()

@app.route('/api/service/<int:request_id>/schedule',methods=['POST'])
def schedule_service(request_id):
    if not _require_role('customer'): return jsonify(success=False,message='Customer login required'),401
    d=request.get_json(silent=True) or {}; when=str(d.get('scheduled_at','')).strip()
    if not when: return jsonify(success=False,message='Choose a date and time.'),400
    try:
        scheduled=datetime.fromisoformat(when.replace('Z',''))
        if scheduled <= datetime.now(): return jsonify(success=False,message='Choose a future date and time.'),400
        when=scheduled.isoformat(timespec='minutes')
    except ValueError:
        return jsonify(success=False,message='Invalid date/time. Use the booking date and time picker.'),400
    c=get_db_connection(); cur=c.execute("UPDATE service_requests SET scheduled_at=?,booking_mode='SCHEDULED',status='SCHEDULED' WHERE id=? AND customer_id=? AND status IN ('PENDING','SEARCHING')",(when,request_id,session['user_id'])); c.commit(); c.close()
    return jsonify(success=bool(cur.rowcount),message='Service scheduled. Provider discovery will open automatically at the scheduled time.' if cur.rowcount else 'This request cannot be scheduled now.')

@app.route('/api/service/<int:request_id>/warranty')
def service_warranty(request_id):
    if 'user_id' not in session: return jsonify(success=False,message='Login required'),401
    c=get_db_connection()
    req,role=_participant_request(c,request_id,session['user_id'])
    if not req: c.close(); return jsonify(success=False,message='You are not part of this service.'),403
    info=c.execute("SELECT sr.status,sr.payment_status,sr.completed_at,sr.paid_at,s.name service_name,pu.name provider_name FROM service_requests sr JOIN services s ON s.id=sr.service_id LEFT JOIN providers p ON p.id=sr.provider_id LEFT JOIN users pu ON pu.id=p.user_id WHERE sr.id=?",(request_id,)).fetchone()
    row=c.execute("""SELECT sw.*, CASE WHEN datetime(sw.warranty_until) >= datetime('now') THEN 1 ELSE 0 END AS active_now,
                     CAST(julianday(sw.warranty_until)-julianday('now') AS INTEGER) AS days_remaining
                     FROM service_warranties sw WHERE sw.request_id=?""",(request_id,)).fetchone(); c.close()
    service={'status':info['status'],'payment_status':info['payment_status'],'completed_at':info['completed_at'],'paid_at':info['paid_at'],'service_name':info['service_name'],'provider_name':info['provider_name']} if info else None
    if not row:
        if info and info['status']=='COMPLETED' and info['payment_status']=='PAID': reason='This service was completed before warranties were introduced, so no warranty record exists.'
        elif info and info['status']=='COMPLETED': reason='The warranty starts once payment for this service is verified.'
        elif info and info['status'] in ('CANCELLED','REJECTED','EXPIRED'): reason='Cancelled services do not carry a warranty.'
        else: reason='The 7-day workmanship warranty is issued automatically after the service is completed and paid.'
        return jsonify(success=True,warranty=None,service=service,reason=reason)
    w=dict(row)
    w['status']='ACTIVE' if w.get('active_now') else 'EXPIRED'
    w['days_remaining']=max(0,int(w.get('days_remaining') or 0)) if w.get('active_now') else 0
    return jsonify(success=True,warranty=w,service=service)

@app.route('/api/provider/<int:provider_id>/favorite')
def favorite_status(provider_id):
    if not _require_role('customer'): return jsonify(success=False),401
    c=get_db_connection(); row=c.execute('SELECT 1 FROM favorite_providers WHERE customer_id=? AND provider_id=?',(session['user_id'],provider_id)).fetchone(); c.close(); return jsonify(success=True,favorited=bool(row))

def _require_role(role):
    return "user_id" in session and session.get("role") == role

def _provider_id_for_user(conn, user_id):
    row=conn.execute("SELECT id FROM providers WHERE user_id=?",(user_id,)).fetchone()
    return int(row["id"]) if row else None

def _trust_score(conn, provider_id):
    row=conn.execute("""
        SELECT p.rating,p.experience,p.skills,p.ekyc_status,u.phone,u.profile_photo_path,p.bio,
        (SELECT COUNT(*) FROM reviews r WHERE r.provider_id=p.id) review_count,
        (SELECT COUNT(*) FROM service_requests sr WHERE sr.provider_id=p.id AND sr.status='COMPLETED') completed
        FROM providers p JOIN users u ON u.id=p.user_id WHERE p.id=?
    """,(provider_id,)).fetchone()
    if not row: return 0
    rating=min(5,max(0,float(row["rating"] or 0)))
    exp=min(10,max(0,int(row["experience"] or 0)))
    completed=min(50,int(row["completed"] or 0))
    completeness=sum(bool(row[k]) for k in ("phone","profile_photo_path","bio","skills","experience","ekyc_status"))/6
    verified=10 if str(row["ekyc_status"] or '').upper() in ('VERIFIED','SUBMITTED') else 0
    return round((rating/5)*45+(exp/10)*15+completeness*15+(completed/50)*15+verified,1)

def _award_provider_metrics(conn, provider_id):
    conn.execute("""
        INSERT INTO provider_metrics(provider_id,accepted_jobs,completed_jobs,declined_jobs,cancelled_jobs)
        VALUES(?,
          (SELECT COUNT(*) FROM service_requests WHERE provider_id=? AND status IN ('ACCEPTED','ARRIVED','IN_PROGRESS','AWAITING_VERIFICATION','AWAITING_PAYMENT','COMPLETED')),
          (SELECT COUNT(*) FROM service_requests WHERE provider_id=? AND status='COMPLETED'),
          0,0)
        ON CONFLICT(provider_id) DO UPDATE SET
          accepted_jobs=excluded.accepted_jobs,
          completed_jobs=excluded.completed_jobs,
          last_calculated_at=CURRENT_TIMESTAMP
    """,(provider_id,provider_id,provider_id))

@app.route('/service/<int:request_id>/live')
def live_service(request_id):
    if 'user_id' not in session: return redirect(url_for('login'))
    conn=get_db_connection()
    row=conn.execute("""SELECT sr.*,s.name service_name,cu.name customer_name,pu.name provider_name,
        pu.phone provider_phone,cu.phone customer_phone,p.rating provider_rating,p.ekyc_status
        FROM service_requests sr JOIN services s ON s.id=sr.service_id JOIN users cu ON cu.id=sr.customer_id
        LEFT JOIN providers p ON p.id=sr.provider_id LEFT JOIN users pu ON pu.id=p.user_id
        WHERE sr.id=?""",(request_id,)).fetchone()
    if not row:
        conn.close(); return "Service not found",404
    provider_user=conn.execute("SELECT user_id FROM providers WHERE id=?",(row['provider_id'],)).fetchone() if row['provider_id'] else None
    if session['user_id'] not in {row['customer_id'], provider_user['user_id'] if provider_user else -1}:
        conn.close(); return "Not authorized",403
    service=dict(row); service['provider_photo_url']=None
    if provider_user:
        pu=conn.execute("SELECT profile_photo_path FROM users WHERE id=?",(provider_user['user_id'],)).fetchone()
        service['provider_photo_url']=media_url(pu['profile_photo_path']) if pu and pu['profile_photo_path'] else None
    conn.close(); return render_template('live_service.html',service=service,role=session['role'])

@app.route('/api/service/<int:request_id>/route')
def service_route(request_id):
    if 'user_id' not in session: return jsonify(success=False,message='Login required'),401
    conn=get_db_connection()
    try:
        data,error=_tracking_snapshot(conn,request_id,session['user_id'])
    finally:
        conn.close()
    if error: return jsonify(success=False,message=error[0]),error[1]
    # Backward-compatible shape for older clients plus the full tracking payload.
    data['customer'].update({'lat':data['customer']['latitude'],'lon':data['customer']['longitude']})
    data['provider'].update({'lat':data['provider']['latitude'],'lon':data['provider']['longitude']})
    return jsonify(data)

@app.route('/provider/earnings')
def provider_earnings():
    if not _require_role('provider'): return redirect(url_for('login'))
    conn=get_db_connection(); pid=_provider_id_for_user(conn,session['user_id'])
    _award_provider_metrics(conn,pid) if pid else None
    rows=conn.execute("SELECT e.*,s.name service_name,cu.name customer_name FROM provider_earnings e LEFT JOIN service_requests sr ON sr.id=e.request_id LEFT JOIN services s ON s.id=sr.service_id LEFT JOIN users cu ON cu.id=sr.customer_id WHERE e.provider_id=? ORDER BY e.created_at DESC LIMIT 100",(pid,)).fetchall() if pid else []
    summary=conn.execute("SELECT COALESCE(SUM(gross_amount),0) gross,COALESCE(SUM(platform_fee),0) fee,COALESCE(SUM(net_amount),0) net,COUNT(*) jobs FROM provider_earnings WHERE provider_id=?",(pid,)).fetchone() if pid else {'gross':0,'fee':0,'net':0,'jobs':0}
    monthly=conn.execute("SELECT strftime('%Y-%m',created_at) month,COALESCE(SUM(net_amount),0) net,COUNT(*) jobs FROM provider_earnings WHERE provider_id=? GROUP BY month ORDER BY month DESC LIMIT 6",(pid,)).fetchall() if pid else []
    pending=conn.execute("SELECT COALESCE(SUM(net_amount),0) net FROM provider_earnings WHERE provider_id=? AND payout_status='PENDING'",(pid,)).fetchone() if pid else {'net':0}
    awaiting=conn.execute("SELECT sr.id,sr.agreed_amount,s.name service_name,sr.status FROM service_requests sr JOIN services s ON s.id=sr.service_id WHERE sr.provider_id=? AND sr.status IN ('AWAITING_VERIFICATION','AWAITING_PAYMENT') ORDER BY sr.created_at DESC",(pid,)).fetchall() if pid else []
    conn.commit(); conn.close(); return render_template('provider_earnings.html',rows=rows,summary=summary,monthly=monthly,pending=pending,awaiting=awaiting)

@app.route('/provider/reviews-center')
def provider_reviews_center():
    if not _require_role('provider'): return redirect(url_for('login'))
    conn=get_db_connection(); pid=_provider_id_for_user(conn,session['user_id'])
    rows=conn.execute("SELECT r.rating,r.review,r.created_at,u.name customer_name,s.name service_name FROM reviews r JOIN users u ON u.id=r.customer_id JOIN service_requests sr ON sr.id=r.request_id JOIN services s ON s.id=sr.service_id WHERE r.provider_id=? ORDER BY r.created_at DESC",(pid,)).fetchall()
    avg=conn.execute("SELECT COALESCE(AVG(rating),0) avg,COUNT(*) cnt FROM reviews WHERE provider_id=?",(pid,)).fetchone()
    dist={r['rating']:r['c'] for r in conn.execute("SELECT rating,COUNT(*) c FROM reviews WHERE provider_id=? GROUP BY rating",(pid,)).fetchall()}
    distribution=[{'stars':s,'count':dist.get(s,0),'pct':round(100*dist.get(s,0)/avg['cnt']) if avg['cnt'] else 0} for s in (5,4,3,2,1)]
    conn.close(); return render_template('provider_reviews_center.html',rows=rows,avg=avg,distribution=distribution)

@app.route('/provider/portfolio',methods=['GET','POST'])
def provider_portfolio():
    if not _require_role('provider'): return redirect(url_for('login'))
    conn=get_db_connection(); pid=_provider_id_for_user(conn,session['user_id'])
    if not pid:
        conn.close(); flash('Provider profile not found.'); return redirect(url_for('provider_dashboard'))
    if request.method=='POST':
        f=request.files.get('image'); title=request.form.get('title','').strip()[:120]; desc=request.form.get('description','').strip()[:500]
        if not title:
            conn.close(); flash('Add a short title for this portfolio item.'); return redirect(url_for('provider_portfolio'))
        try:
            rel_path=_save_public_image(f,f"portfolio_{pid}")
        except ValueError as exc:
            conn.close(); flash(str(exc)); return redirect(url_for('provider_portfolio'))
        conn.execute("INSERT INTO portfolio_items(provider_id,title,description,image_path) VALUES(?,?,?,?)",(pid,title,desc,rel_path)); conn.commit(); conn.close()
        flash('Portfolio item added. Customers can now see it on your profile.'); return redirect(url_for('provider_portfolio'))
    rows=conn.execute('SELECT * FROM portfolio_items WHERE provider_id=? ORDER BY created_at DESC',(pid,)).fetchall(); conn.close()
    return render_template('provider_portfolio.html',rows=rows,provider_id=pid)

@app.route('/provider/portfolio/<int:item_id>/delete',methods=['POST'])
def provider_portfolio_delete(item_id):
    if not _require_role('provider'):
        return jsonify(success=False,message='Provider login required'),401
    conn=get_db_connection(); pid=_provider_id_for_user(conn,session['user_id'])
    row=conn.execute('SELECT id,image_path FROM portfolio_items WHERE id=? AND provider_id=?',(item_id,pid)).fetchone()
    if not row:
        conn.close()
        if request.is_json or request.headers.get('X-Requested-With')=='fetch':
            return jsonify(success=False,message='Portfolio item not found.'),404
        flash('Portfolio item not found.'); return redirect(url_for('provider_portfolio'))
    conn.execute('DELETE FROM portfolio_items WHERE id=? AND provider_id=?',(item_id,pid)); conn.commit(); conn.close()
    _delete_media(row['image_path'])
    if request.is_json or request.headers.get('X-Requested-With')=='fetch':
        return jsonify(success=True,message='Portfolio item removed.')
    flash('Portfolio item removed.'); return redirect(url_for('provider_portfolio'))

@app.route('/favorites/<int:provider_id>/toggle',methods=['POST'])
def toggle_favorite(provider_id):
    if not _require_role('customer'): return jsonify(success=False,message='Customer login required'),401
    conn=get_db_connection(); existing=conn.execute('SELECT id FROM favorite_providers WHERE customer_id=? AND provider_id=?',(session['user_id'],provider_id)).fetchone()
    if existing: conn.execute('DELETE FROM favorite_providers WHERE id=?',(existing['id'],)); saved=False
    else: conn.execute('INSERT OR IGNORE INTO favorite_providers(customer_id,provider_id) VALUES(?,?)',(session['user_id'],provider_id)); saved=True
    conn.commit(); conn.close(); return jsonify(success=True,favorited=saved)

@app.route('/api/provider/<int:provider_id>/portfolio')
def provider_portfolio_api(provider_id):
    if 'user_id' not in session: return jsonify(success=False,message='Login required'),401
    conn=get_db_connection(); rows=conn.execute('SELECT p.title,p.description,p.image_path,p.created_at FROM portfolio_items p JOIN providers pr ON pr.id=p.provider_id WHERE p.provider_id=? AND pr.approved=1 ORDER BY p.created_at DESC',(provider_id,)).fetchall(); conn.close()
    items=[]
    for r in rows:
        d=dict(r); d['image_url']=media_url(d.pop('image_path'))
        items.append(d)
    return jsonify(success=True,items=items)

def _normalize_phone(raw):
    value = str(raw or "").strip()
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 10 or len(digits) > 15:
        return None
    return ("+" + digits) if value.startswith("+") else digits


def _participant_request(conn, request_id, user_id):
    """Return (row, role) if user_id is the customer or assigned provider of request_id."""
    row = conn.execute("""
        SELECT sr.id, sr.customer_id, sr.provider_id, sr.status, sr.service_id, p.user_id AS provider_user_id
        FROM service_requests sr LEFT JOIN providers p ON p.id = sr.provider_id WHERE sr.id = ?
    """, (request_id,)).fetchone()
    if not row:
        return None, None
    if int(row["customer_id"]) == int(user_id):
        return row, "customer"
    if row["provider_user_id"] is not None and int(row["provider_user_id"]) == int(user_id):
        return row, "provider"
    return None, None


@app.route('/api/trusted-contact', methods=['GET', 'POST'])
def trusted_contact():
    if 'user_id' not in session: return jsonify(success=False,message='Login required'),401
    conn=get_db_connection()
    if request.method=='POST':
        d=request.get_json(silent=True) or {}
        name=str(d.get('name','')).strip()[:100]; rel=str(d.get('relationship','')).strip()[:50]
        phone=_normalize_phone(d.get('phone'))
        if len(name)<2: conn.close(); return jsonify(success=False,message='Enter the contact\'s name (at least 2 characters).'),400
        if not phone: conn.close(); return jsonify(success=False,message='Enter a valid phone number with 10 to 15 digits.'),400
        count=conn.execute('SELECT COUNT(*) c FROM trusted_contacts WHERE user_id=?',(session['user_id'],)).fetchone()['c']
        existing=conn.execute('SELECT id FROM trusted_contacts WHERE user_id=? AND phone=?',(session['user_id'],phone)).fetchone()
        if existing:
            conn.execute('UPDATE trusted_contacts SET name=?,relationship=? WHERE id=? AND user_id=?',(name,rel or None,existing['id'],session['user_id']))
            message='Trusted contact updated.'
        else:
            if count>=5: conn.close(); return jsonify(success=False,message='You can save up to 5 trusted contacts. Remove one to add another.'),400
            conn.execute('INSERT INTO trusted_contacts(user_id,name,phone,relationship) VALUES(?,?,?,?)',(session['user_id'],name,phone,rel or None))
            message='Trusted contact saved.'
        # Mirror the most recent contact onto the user row for legacy readers.
        conn.execute('UPDATE users SET trusted_contact_name=?,trusted_contact_phone=? WHERE id=?',(name,phone,session['user_id']))
        conn.commit()
    contacts=conn.execute('SELECT id,name,phone,relationship,created_at FROM trusted_contacts WHERE user_id=? ORDER BY created_at DESC',(session['user_id'],)).fetchall()
    conn.close()
    return jsonify(success=True,message=message if request.method=='POST' else None,trusted_contacts=[dict(c) for c in contacts])


@app.route('/api/sos/<int:request_id>', methods=['POST'])
def sos(request_id):
    if 'user_id' not in session: return jsonify(success=False,message='Login required'),401
    d=request.get_json(silent=True) or {}
    conn=get_db_connection()
    row,role=_participant_request(conn,request_id,session['user_id'])
    if not row: conn.close(); return jsonify(success=False,message='You are not part of this service.'),403
    if row['status'] not in TRACKING_ACTIVE_STATUSES:
        conn.close(); return jsonify(success=False,message='SOS is available only while a service is active. If you are in danger, call 112.'),409
    recent=conn.execute("SELECT id FROM sos_events WHERE request_id=? AND user_id=? AND status='OPEN' AND created_at>=datetime('now','-2 minute')",(request_id,session['user_id'])).fetchone()
    if recent:
        conn.close(); return jsonify(success=True,message='Your SOS alert is already open and the operations team has been notified. Call 112 if you are in immediate danger.',emergency_number='112',sos_id=recent['id'],duplicate=True)
    lat=lon=None
    try:
        if d.get('latitude') is not None and d.get('longitude') is not None:
            lat=float(d['latitude']); lon=float(d['longitude'])
            if not (-90<=lat<=90 and -180<=lon<=180): lat=lon=None
    except (TypeError,ValueError): lat=lon=None
    if lat is None:
        u=conn.execute('SELECT latitude,longitude,location_updated_at FROM users WHERE id=?',(session['user_id'],)).fetchone()
        age=_location_age_seconds(u['location_updated_at']) if u else None
        if u and u['latitude'] is not None and age is not None and age<=STALE_LOCATION_SECONDS:
            lat,lon=float(u['latitude']),float(u['longitude'])
    note=str(d.get('message','')).strip()[:300]
    contact=conn.execute("SELECT name,phone,relationship FROM trusted_contacts WHERE user_id=? ORDER BY created_at DESC LIMIT 1",(session['user_id'],)).fetchone()
    service=conn.execute("SELECT s.name FROM services s WHERE s.id=?",(row['service_id'],)).fetchone()
    cur=conn.execute("INSERT INTO sos_events(request_id,user_id,role,latitude,longitude,message,trusted_contact_name,trusted_contact_phone) VALUES(?,?,?,?,?,?,?,?)",
                     (request_id,session['user_id'],role,lat,lon,note or None,contact['name'] if contact else None,contact['phone'] if contact else None))
    sos_id=cur.lastrowid
    who='Customer' if role=='customer' else 'Provider'
    alert_message=f"{who} {session.get('user_name','')} triggered SOS on service #{request_id} ({service['name'] if service else 'service'})."
    if lat is not None: alert_message+=f" Last known location {lat:.5f}, {lon:.5f}."
    else: alert_message+=" No recent GPS location available."
    if note: alert_message+=f" Note: {note}"
    conn.execute("INSERT INTO admin_alerts(severity,title,message,request_id,user_id) VALUES('HIGH','SOS triggered',?,?,?)",(alert_message,request_id,session['user_id']))
    _log_event(conn,request_id,session['user_id'],'SOS_TRIGGERED',alert_message,{'sos_id':sos_id,'latitude':lat,'longitude':lon})
    conn.commit(); conn.close()
    if socketio:
        socketio.emit('sos_alert',{'sos_id':sos_id,'request_id':request_id,'role':role,'message':alert_message,'latitude':lat,'longitude':lon},to='admins')
    return jsonify(success=True,sos_id=sos_id,message='SOS alert sent to SmartServe operations with your service details' + (' and last known location.' if lat is not None else '. Share your location for faster help.') + ' If you are in immediate danger, call 112 now.',emergency_number='112',trusted_contact=dict(contact) if contact else None,location_included=lat is not None)


@app.route('/api/sos/mine')
def my_sos_events():
    if 'user_id' not in session: return jsonify(success=False,message='Login required'),401
    conn=get_db_connection()
    rows=conn.execute("SELECT e.id,e.request_id,e.status,e.created_at,e.acknowledged_at,e.latitude,e.longitude,s.name service_name FROM sos_events e JOIN service_requests sr ON sr.id=e.request_id JOIN services s ON s.id=sr.service_id WHERE e.user_id=? ORDER BY e.created_at DESC LIMIT 20",(session['user_id'],)).fetchall()
    conn.close(); return jsonify(success=True,events=[dict(r) for r in rows])


DISPUTE_REASONS=['Incomplete work','Damage to property','Overcharging','Provider did not arrive','Customer not available','Payment issue','Unprofessional behaviour','Safety concern','Other']
DISPUTE_ELIGIBLE_STATUSES=TRACKING_ACTIVE_STATUSES+('COMPLETED','CANCELLED','REJECTED')


@app.route('/api/disputes', methods=['GET','POST'])
def create_dispute():
    if 'user_id' not in session: return jsonify(success=False,message='Login required'),401
    conn=get_db_connection()
    if request.method=='POST':
        d=request.get_json(silent=True) or {}
        try: rid=int(d.get('request_id') or 0)
        except (TypeError,ValueError): rid=0
        reason=str(d.get('reason','')).strip()[:120]; desc=str(d.get('description','')).strip()[:1000]
        row,role=_participant_request(conn,rid,session['user_id'])
        if not row: conn.close(); return jsonify(success=False,message='Select one of your own services.'),403
        if row['status'] not in DISPUTE_ELIGIBLE_STATUSES: conn.close(); return jsonify(success=False,message='A dispute can be opened only for an assigned, active or completed service.'),409
        if reason not in DISPUTE_REASONS: conn.close(); return jsonify(success=False,message='Choose a valid issue type.'),400
        if len(desc)<10: conn.close(); return jsonify(success=False,message='Describe the issue in at least 10 characters so our team can help.'),400
        if conn.execute("SELECT id FROM disputes WHERE request_id=? AND opened_by=? AND status='OPEN'",(rid,session['user_id'])).fetchone():
            conn.close(); return jsonify(success=False,message='You already have an open dispute for this service. Our team will contact you.'),409
        cur=conn.execute('INSERT INTO disputes(request_id,opened_by,reason,description) VALUES(?,?,?,?)',(rid,session['user_id'],reason,desc))
        dispute_id=cur.lastrowid
        conn.execute("INSERT INTO admin_alerts(severity,title,message,request_id,user_id) VALUES('MEDIUM','New dispute',?,?,?)",(f"{reason} — reported by {'customer' if role=='customer' else 'provider'} on service #{rid}: {desc[:160]}",rid,session['user_id']))
        _log_event(conn,rid,session['user_id'],'DISPUTE_OPENED',reason,{'dispute_id':dispute_id})
        conn.commit()
        if socketio: socketio.emit('dispute_opened',{'dispute_id':dispute_id,'request_id':rid,'reason':reason},to='admins')
        message=f'Dispute #{dispute_id} opened. SmartServe operations will review it and contact you.'
    rows=conn.execute("SELECT d.id,d.request_id,d.reason,d.description,d.status,d.resolution,d.created_at,d.updated_at,s.name service_name FROM disputes d JOIN service_requests sr ON sr.id=d.request_id JOIN services s ON s.id=sr.service_id WHERE d.opened_by=? ORDER BY d.created_at DESC LIMIT 30",(session['user_id'],)).fetchall()
    conn.close()
    return jsonify(success=True,message=message if request.method=='POST' else None,disputes=[dict(r) for r in rows],reasons=DISPUTE_REASONS)


@app.route('/api/safety-tools', methods=['GET'])
def safety_tools():
    if 'user_id' not in session:
        return jsonify(success=False, message='Login required'), 401
    conn = get_db_connection()
    u = conn.execute('SELECT name, preferred_language, low_bandwidth_mode, phone FROM users WHERE id=?', (session['user_id'],)).fetchone()
    contacts = conn.execute('SELECT id,name,phone,relationship,created_at FROM trusted_contacts WHERE user_id=? ORDER BY created_at DESC', (session['user_id'],)).fetchall()
    active_statuses = "('ASSIGNED','ACCEPTED','ARRIVED','IN_PROGRESS','AWAITING_VERIFICATION','AWAITING_PAYMENT')"
    if session.get('role') == 'customer':
        base = """SELECT sr.id,sr.status,sr.route_status,sr.created_at,sr.completed_at,sr.warranty_days,sr.warranty_until,sr.payment_status,
                     s.name service_name,pu.name provider_name,pu.phone provider_phone,
                     (SELECT COUNT(*) FROM disputes d WHERE d.request_id=sr.id AND d.status='OPEN') open_disputes
              FROM service_requests sr JOIN services s ON s.id=sr.service_id
              LEFT JOIN providers p ON p.id=sr.provider_id LEFT JOIN users pu ON pu.id=p.user_id
              WHERE sr.customer_id=? """
    else:
        base = """SELECT sr.id,sr.status,sr.route_status,sr.created_at,sr.completed_at,sr.warranty_days,sr.warranty_until,sr.payment_status,
                     s.name service_name,cu.name customer_name,cu.phone customer_phone,
                     (SELECT COUNT(*) FROM disputes d WHERE d.request_id=sr.id AND d.status='OPEN') open_disputes
              FROM service_requests sr JOIN services s ON s.id=sr.service_id
              JOIN providers p ON p.id=sr.provider_id JOIN users cu ON cu.id=sr.customer_id
              WHERE p.user_id=? """
    active = conn.execute(base + f"AND sr.status IN {active_statuses} ORDER BY sr.created_at DESC LIMIT 20", (session['user_id'],)).fetchall()
    completed = conn.execute(base + "AND sr.status='COMPLETED' ORDER BY COALESCE(sr.completed_at,sr.created_at) DESC LIMIT 20", (session['user_id'],)).fetchall()
    disputes = conn.execute("SELECT d.id,d.request_id,d.reason,d.status,d.resolution,d.created_at,s.name service_name FROM disputes d JOIN service_requests sr ON sr.id=d.request_id JOIN services s ON s.id=sr.service_id WHERE d.opened_by=? ORDER BY d.created_at DESC LIMIT 20", (session['user_id'],)).fetchall()
    sos_rows = conn.execute("SELECT e.id,e.request_id,e.status,e.created_at,e.acknowledged_at FROM sos_events e WHERE e.user_id=? ORDER BY e.created_at DESC LIMIT 10", (session['user_id'],)).fetchall()
    warranties = conn.execute("""SELECT sw.request_id,sw.warranty_days,sw.warranty_until,sw.status,sw.terms,sw.created_at,s.name service_name,
                                 CASE WHEN datetime(sw.warranty_until) >= datetime('now') THEN 1 ELSE 0 END AS active_now
                                 FROM service_warranties sw JOIN service_requests sr ON sr.id=sw.request_id JOIN services s ON s.id=sr.service_id
                                 WHERE sr.customer_id=? OR sr.provider_id IN (SELECT id FROM providers WHERE user_id=?)
                                 ORDER BY sw.created_at DESC LIMIT 20""", (session['user_id'], session['user_id'])).fetchall()
    conn.close()
    return jsonify(success=True, role=session.get('role'), user=dict(u) if u else {},
                   trusted_contacts=[dict(x) for x in contacts], active_services=[dict(x) for x in active],
                   completed_services=[dict(x) for x in completed], disputes=[dict(x) for x in disputes],
                   sos_events=[dict(x) for x in sos_rows], warranties=[dict(x) for x in warranties],
                   dispute_reasons=DISPUTE_REASONS, emergency_number='112')


@app.route('/api/trusted-contact/<int:contact_id>', methods=['DELETE'])
def delete_trusted_contact(contact_id):
    if 'user_id' not in session:
        return jsonify(success=False, message='Login required'), 401
    conn=get_db_connection()
    cur=conn.execute('DELETE FROM trusted_contacts WHERE id=? AND user_id=?',(contact_id,session['user_id']))
    latest=conn.execute('SELECT name,phone FROM trusted_contacts WHERE user_id=? ORDER BY created_at DESC LIMIT 1',(session['user_id'],)).fetchone()
    conn.execute('UPDATE users SET trusted_contact_name=?,trusted_contact_phone=? WHERE id=?',(latest['name'] if latest else None,latest['phone'] if latest else None,session['user_id']))
    conn.commit(); conn.close()
    return jsonify(success=bool(cur.rowcount), message='Trusted contact removed.' if cur.rowcount else 'Contact not found.'), (200 if cur.rowcount else 404)

@app.route('/api/preferences',methods=['POST'])
def preferences():
    if 'user_id' not in session: return jsonify(success=False,message='Login required'),401
    d=request.get_json(silent=True) or {}; lang=str(d.get('language','en'))[:10]; low=1 if d.get('low_bandwidth') else 0
    conn=get_db_connection(); conn.execute('UPDATE users SET preferred_language=?,low_bandwidth_mode=? WHERE id=?',(lang,low,session['user_id'])); conn.commit(); conn.close(); return jsonify(success=True)

REPEAT_FREQUENCIES={'WEEKLY':'+7 day','MONTHLY':'+30 day','QUARTERLY':'+90 day'}

@app.route('/bookings/repeat',methods=['GET','POST'])
def repeat_booking():
    if not _require_role('customer'): return jsonify(success=False,message='Customer login required'),401
    conn=get_db_connection()
    if request.method=='POST':
        d=request.get_json(silent=True) or {}
        try: rid=int(d.get('request_id') or 0)
        except (TypeError,ValueError): rid=0
        freq=str(d.get('frequency','MONTHLY')).upper(); notes=str(d.get('notes','')).strip()[:500]
        if freq not in REPEAT_FREQUENCIES: conn.close(); return jsonify(success=False,message='Choose weekly, monthly or quarterly.'),400
        row=conn.execute("SELECT sr.service_id,sr.provider_id,sr.status,s.name service_name FROM service_requests sr JOIN services s ON s.id=sr.service_id WHERE sr.id=? AND sr.customer_id=?",(rid,session['user_id'])).fetchone()
        if not row: conn.close(); return jsonify(success=False,message='Choose one of your own bookings.'),404
        if row['status']!='COMPLETED': conn.close(); return jsonify(success=False,message='Repeat plans can only be created from completed bookings.'),409
        if conn.execute("SELECT id FROM recurring_bookings WHERE customer_id=? AND service_id=? AND COALESCE(provider_id,0)=COALESCE(?,0) AND active=1",(session['user_id'],row['service_id'],row['provider_id'])).fetchone():
            conn.close(); return jsonify(success=False,message='You already have an active repeat plan for this service and provider.'),409
        conn.execute(f"INSERT INTO recurring_bookings(customer_id,provider_id,service_id,frequency,next_run_at,notes) VALUES(?,?,?,?,datetime('now','{REPEAT_FREQUENCIES[freq]}'),?)",(session['user_id'],row['provider_id'],row['service_id'],freq,notes))
        conn.commit()
        message=f"{freq.capitalize()} repeat plan created for {row['service_name']}. Upcoming visits appear on your dashboard with a one-tap rebook."
    rows=conn.execute("SELECT rb.id,rb.frequency,rb.next_run_at,CASE WHEN rb.active=1 THEN 'ACTIVE' ELSE 'CANCELLED' END status,rb.notes,rb.created_at,s.name service_name,pu.name provider_name FROM recurring_bookings rb JOIN services s ON s.id=rb.service_id LEFT JOIN providers p ON p.id=rb.provider_id LEFT JOIN users pu ON pu.id=p.user_id WHERE rb.customer_id=? ORDER BY rb.created_at DESC",(session['user_id'],)).fetchall()
    conn.close()
    return jsonify(success=True,message=message if request.method=='POST' else None,plans=[dict(r) for r in rows])

@app.route('/bookings/repeat/<int:plan_id>/cancel',methods=['POST'])
def cancel_repeat_booking(plan_id):
    if not _require_role('customer'): return jsonify(success=False,message='Customer login required'),401
    conn=get_db_connection(); cur=conn.execute("UPDATE recurring_bookings SET active=0 WHERE id=? AND customer_id=? AND active=1",(plan_id,session['user_id'])); conn.commit(); conn.close()
    return (jsonify(success=True,message='Repeat plan cancelled.') if cur.rowcount else (jsonify(success=False,message='Plan not found.'),404))

@app.route('/api/favorites')
def favorites_list():
    if not _require_role('customer'): return jsonify(success=False,message='Customer login required'),401
    conn=get_db_connection()
    rows=conn.execute("""SELECT p.id provider_id,p.rating,p.experience,p.skills,u.name,u.is_online,u.profile_photo_path,
        (SELECT COUNT(*) FROM reviews rv WHERE rv.provider_id=p.id) review_count
        FROM favorite_providers f JOIN providers p ON p.id=f.provider_id JOIN users u ON u.id=p.user_id WHERE f.customer_id=? ORDER BY f.created_at DESC""",(session['user_id'],)).fetchall()
    conn.close()
    out=[]
    for r in rows:
        d=dict(r); d['photo_url']=media_url(d.pop('profile_photo_path')); out.append(d)
    return jsonify(success=True,favorites=out)

@app.route('/admin/operations')
def admin_operations():
    if not _require_role('admin'): return redirect(url_for('login'))
    conn=get_db_connection()
    stats={
      'users':conn.execute("SELECT COUNT(*) c FROM users").fetchone()['c'],
      'online_providers':conn.execute("SELECT COUNT(*) c FROM users WHERE role='provider' AND is_online=1").fetchone()['c'],
      'active_services':conn.execute("SELECT COUNT(*) c FROM service_requests WHERE status IN ('ASSIGNED','ACCEPTED','ARRIVED','IN_PROGRESS','AWAITING_VERIFICATION','AWAITING_PAYMENT')").fetchone()['c'],
      'open_sos':conn.execute("SELECT COUNT(*) c FROM sos_events WHERE status='OPEN'").fetchone()['c'],
      'open_disputes':conn.execute("SELECT COUNT(*) c FROM disputes WHERE status='OPEN'").fetchone()['c'],
      'open_fraud':conn.execute("SELECT COUNT(*) c FROM fraud_signals WHERE status='OPEN'").fetchone()['c'],
      'alerts':conn.execute("SELECT COUNT(*) c FROM admin_alerts WHERE status='OPEN'").fetchone()['c'],
    }
    sos_events=conn.execute("""SELECT e.*,u.name user_name,u.phone user_phone,u.role user_role,s.name service_name,sr.status service_status
        FROM sos_events e JOIN users u ON u.id=e.user_id JOIN service_requests sr ON sr.id=e.request_id JOIN services s ON s.id=sr.service_id
        WHERE e.status='OPEN' ORDER BY e.created_at DESC LIMIT 30""").fetchall()
    alerts=conn.execute("SELECT a.*,u.name user_name FROM admin_alerts a LEFT JOIN users u ON u.id=a.user_id WHERE a.status='OPEN' ORDER BY a.created_at DESC LIMIT 30").fetchall()
    fraud=conn.execute("SELECT * FROM fraud_signals WHERE status='OPEN' ORDER BY created_at DESC LIMIT 30").fetchall()
    disputes=conn.execute("""SELECT d.*,u.name opened_by_name,u.role opened_by_role,s.name service_name FROM disputes d JOIN users u ON u.id=d.opened_by
        JOIN service_requests sr ON sr.id=d.request_id JOIN services s ON s.id=sr.service_id WHERE d.status='OPEN' ORDER BY d.created_at DESC LIMIT 30""").fetchall()
    pending_kyc=conn.execute("SELECT p.id,p.ekyc_status,u.name FROM providers p JOIN users u ON u.id=p.user_id WHERE p.ekyc_status='SUBMITTED' ORDER BY p.id DESC LIMIT 30").fetchall()
    conn.close()
    return render_template('admin_operations.html',stats=stats,alerts=alerts,fraud=fraud,disputes=disputes,sos_events=sos_events,pending_kyc=pending_kyc)

@app.route('/admin/sos/<int:sos_id>/acknowledge', methods=['POST'])
def admin_sos_acknowledge(sos_id):
    if not _require_role('admin'): return redirect(url_for('login'))
    c=get_db_connection()
    c.execute("UPDATE sos_events SET status='ACKNOWLEDGED',acknowledged_at=CURRENT_TIMESTAMP WHERE id=?",(sos_id,))
    row=c.execute("SELECT request_id,user_id FROM sos_events WHERE id=?",(sos_id,)).fetchone()
    if row:
        c.execute("UPDATE admin_alerts SET status='RESOLVED',resolved_at=CURRENT_TIMESTAMP WHERE request_id=? AND user_id=? AND title='SOS triggered' AND status='OPEN'",(row['request_id'],row['user_id']))
        _log_event(c,int(row['request_id']),session['user_id'],'SOS_ACKNOWLEDGED','Operations team acknowledged the SOS alert.',{'sos_id':sos_id})
    c.commit(); c.close()
    if socketio and row: socketio.emit('sos_acknowledged',{'sos_id':sos_id,'request_id':row['request_id']},to=f"user:{row['user_id']}")
    flash('SOS alert acknowledged.'); return redirect(url_for('admin_operations'))

@app.route('/admin/alert/<int:alert_id>/resolve', methods=['POST'])
def admin_alert_resolve(alert_id):
    if not _require_role('admin'): return redirect(url_for('login'))
    c=get_db_connection(); c.execute("UPDATE admin_alerts SET status='RESOLVED',resolved_at=CURRENT_TIMESTAMP WHERE id=?",(alert_id,)); c.commit(); c.close()
    return redirect(url_for('admin_operations'))

@app.route('/admin/kyc/<int:provider_id>/reject', methods=['POST'])
def admin_reject_kyc(provider_id):
    if not _require_role('admin'): return redirect(url_for('login'))
    c=get_db_connection(); c.execute("UPDATE providers SET ekyc_status='REJECTED' WHERE id=?",(provider_id,)); c.execute("DELETE FROM provider_badges WHERE provider_id=? AND badge_key IN ('identity_submitted','identity_verified')",(provider_id,)); c.commit(); c.close()
    flash('Provider identity document rejected.'); return redirect(url_for('admin_operations'))

@app.route('/admin/fraud-scan')
def fraud_scan():
    if not _require_role('admin'): return redirect(url_for('login'))
    conn=get_db_connection()
    # Explainable starter rules: repeated cancellations and unusually high review volume.
    users=conn.execute("SELECT id FROM users").fetchall()
    for u in users:
        uid=u['id']; canc=conn.execute("SELECT COUNT(*) c FROM service_requests WHERE customer_id=? AND status='CANCELLED' AND created_at>=datetime('now','-30 day')",(uid,)).fetchone()['c']
        if canc>=5: conn.execute("INSERT INTO fraud_signals(user_id,signal_type,severity,details) SELECT ?, 'HIGH_CANCELLATION_RATE','MEDIUM',? WHERE NOT EXISTS (SELECT 1 FROM fraud_signals WHERE user_id=? AND signal_type='HIGH_CANCELLATION_RATE' AND status='OPEN')",(uid,f'{canc} cancellations in 30 days',uid))
    conn.commit(); conn.close(); flash('Fraud scan completed using explainable baseline rules.'); return redirect(url_for('admin_operations'))


def _activate_due_scheduled_bookings():
    """Small opportunistic scheduler for web workers; safe to call frequently."""
    conn=None
    activated=[]
    try:
        conn=get_db_connection()
        rows=conn.execute("SELECT id,customer_id FROM service_requests WHERE status='SCHEDULED' AND scheduled_at IS NOT NULL AND datetime(scheduled_at) <= datetime('now') LIMIT 50").fetchall()
        for row in rows:
            cur=conn.execute("UPDATE service_requests SET status='SEARCHING',booking_mode='ON_DEMAND',search_started_at=COALESCE(search_started_at,CURRENT_TIMESTAMP) WHERE id=? AND status='SCHEDULED'",(row['id'],))
            if cur.rowcount:
                activated.append((int(row['id']),int(row['customer_id'])))
                _log_event(conn,int(row['id']),int(row['customer_id']),'SCHEDULED_SERVICE_ACTIVATED','Scheduled service is now ready for provider selection.')
        conn.commit()
    except Exception as exc:
        if conn:
            try: conn.rollback()
            except Exception: pass
        print('SCHEDULE ACTIVATION WARNING:',repr(exc))
    finally:
        if conn:
            conn.close()
    for rid,uid in activated:
        if socketio: socketio.emit('scheduled_service_ready',{'request_id':rid,'status':'SEARCHING'},to=f'user:{uid}')
    return len(activated)


_last_schedule_check=0.0
_schedule_check_lock=threading.Lock()

@app.before_request
def smartserve_housekeeping():
    global _last_schedule_check
    if request.path.startswith('/static/'):
        return
    if not any(request.path.startswith(prefix) for prefix in ('/customer/', '/provider/', '/searching/', '/api/matching/', '/api/safety-tools', '/features', '/request-service')):
        return
    now=time.monotonic()
    if now-_last_schedule_check < 3.0:
        return
    with _schedule_check_lock:
        now=time.monotonic()
        if now-_last_schedule_check >= 3.0:
            _last_schedule_check=now
            _activate_due_scheduled_bookings()


# ---------------- SCHEDULED BOOKING WORKER ----------------
def _scheduled_booking_worker():
    """Activate due scheduled bookings without holding a DB connection between cycles."""
    while True:
        try:
            conn=get_db_connection()
            rows=conn.execute("SELECT id,customer_id FROM service_requests WHERE status='SCHEDULED' AND scheduled_at IS NOT NULL AND datetime(scheduled_at) <= datetime('now') LIMIT 50").fetchall()
            for row in rows:
                cur=conn.execute("UPDATE service_requests SET status='SEARCHING',booking_mode='ON_DEMAND',search_started_at=COALESCE(search_started_at,CURRENT_TIMESTAMP) WHERE id=? AND status='SCHEDULED'",(row['id'],))
                if cur.rowcount:
                    _log_event(conn,int(row['id']),int(row['customer_id']),'SCHEDULED_SERVICE_ACTIVATED','Scheduled service is now ready for provider selection.')
            conn.commit()
            conn.close()
            for row in rows:
                if socketio:
                    socketio.emit('scheduled_service_ready',{'request_id':int(row['id']),'status':'SEARCHING'},to=f"user:{int(row['customer_id'])}")
                    _emit_request_update(int(row['id']),event='scheduled_service_ready')
        except Exception as exc:
            print('SCHEDULE WORKER WARNING:', repr(exc))
        time.sleep(5)


def _start_background_workers():
    worker=threading.Thread(target=_scheduled_booking_worker,name='smartserve-scheduler',daemon=True)
    worker.start()


if __name__ == "__main__":
    _start_background_workers()
    host = "0.0.0.0"
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    if socketio:
        socketio.run(app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=True)
    else:
        app.run(host=host, port=port, debug=debug)