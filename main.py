from flask import Flask, request, jsonify, send_from_directory
import firebase_admin
from firebase_admin import credentials, db
from datetime import datetime, timezone
import time
import threading
import uuid
import json
import os
import sys

# ── Load settings ──────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")
with open(SETTINGS_PATH) as f:
    settings = json.load(f)

# ── Firebase credentials ────────────────────────────────────────────────────────
service_key_path = settings["firebase"]["serviceKeyPath"]
if not os.path.isabs(service_key_path):
    service_key_path = os.path.join(BASE_DIR, service_key_path)
cred = credentials.Certificate(service_key_path)
firebase_admin.initialize_app(cred, {
    "databaseURL": settings["firebase"]["databaseURL"]
})

app = Flask(__name__)

UPLOAD_FOLDER = os.path.join(BASE_DIR, "media")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ── Server event logger ─────────────────────────────────────────────────────────
def log_server_event(action, user_name, email, user_uid, ip, result, reason=None):
    now = datetime.now(timezone.utc)
    date_key = now.strftime("%d_%m_%Y")
    hour_minute = now.strftime("%H:%M")
    second = now.strftime("%S")

    log_entry = {
        "action": action,
        "userName": user_name or "unknown",
        "email": email or "",
        "userUID": user_uid or "",
        "ip": ip or "",
        "result": result,
        "timestamp": int(time.time())
    }
    if reason:
        log_entry["reason"] = reason

    ref = db.reference(f"/server_logs/{date_key}/{hour_minute}")
    ref.child(f"S:{second}").set(log_entry)


# ── Remote server control ───────────────────────────────────────────────────────
@app.route("/server/control", methods=["POST"])
def server_control():
    data = request.get_json(force=True)
    action = data.get("action", "").lower()
    provided_key = data.get("shutdownKey", "")
    user_name = data.get("userName", "")
    email = data.get("email", "")
    user_uid = data.get("userUID", "")
    caller_ip = request.remote_addr or ""

    # 1. Superadmin check
    if email and action!="status":
        safe_email = email.replace(".", "_")
        role_level = db.reference(f"/users/{safe_email}/roleLevel").get()
        if role_level is None or role_level < 2:
            log_server_event(action, user_name, email, user_uid, caller_ip, "rejected", "Superadmin access required")
            return jsonify(status="rejected", reason="Superadmin access required"), 403

    # 2. Admin blacklist check
    blacklist = (db.reference("/server_admin_blacklist").get()) or {}
    blacklisted = bool(
        (user_uid and blacklist.get("users", {}).get(user_uid)) or
        (user_name and blacklist.get("usernames", {}).get(user_name)) or
        (email and blacklist.get("emails", {}).get(email))
    )
    if blacklisted:
        if email:
            db.reference(f"/users/{email.replace('.', '_')}/roleLevel").set(0)
        log_server_event(action, user_name, email, user_uid, caller_ip, "rejected", "Admin is blacklisted — demoted to user")
        return jsonify(status="rejected", reason="Admin is blacklisted — demoted to user"), 403

    # 3. Admin whitelist check (only if whitelist has entries)
    whitelist = (db.reference("/server_whitelist").get()) or {}
    has_entries = any(whitelist.get(k, {}) for k in ["ips", "users", "usernames", "emails"])
    if has_entries:
        in_wl = bool(
            (caller_ip and whitelist.get("ips", {}).get(caller_ip)) or
            (user_uid and whitelist.get("users", {}).get(user_uid)) or
            (user_name and whitelist.get("usernames", {}).get(user_name)) or
            (email and whitelist.get("emails", {}).get(email))
        )
        if not in_wl:
            log_server_event(action, user_name, email, user_uid, caller_ip, "rejected", "Not in admin whitelist")
            return jsonify(status="rejected", reason="Not in admin whitelist"), 403

    # 4. Existing settings.json remote control checks
    rc = settings.get("remoteControl", {})
    if not rc.get("allowRemoteControl", False):
        log_server_event(action, user_name, email, user_uid, caller_ip, "rejected", "Remote control is disabled")
        return jsonify(status="rejected", reason="Remote control is disabled"), 403

    allowed_ips = rc.get("allowedRemoteIPs", [])
    if allowed_ips and caller_ip not in allowed_ips:
        log_server_event(action, user_name, email, user_uid, caller_ip, "rejected", "IP not allowed")
        return jsonify(status="rejected", reason="IP not allowed", ip=caller_ip), 403

    # 5. Handle actions
    if action == "status":
        return jsonify(
            status="ok",
            enabled=settings["server"].get("enabled", True),
            host=settings["server"].get("host"),
            port=settings["server"].get("port"),
            uptime=int(time.time())
        ), 200

    if action == "disable":
        expected_key = settings["server"].get("shutdownKey", "")
        if provided_key != expected_key and expected_key:
            log_server_event(action, user_name, email, user_uid, caller_ip, "rejected", "Invalid shutdown key")
            return jsonify(status="rejected", reason="Invalid shutdown key"), 401

        if not settings["server"].get("enabled", True):
            log_server_event(action, user_name, email, user_uid, caller_ip, "success")
            return jsonify(status="ok", message="Server already disabled"), 200

        settings["server"]["enabled"] = False
        with open(SETTINGS_PATH, "w") as f:
            json.dump(settings, f, indent=2)

        log_server_event(action, user_name, email, user_uid, caller_ip, "success")

        def shutdown():
            time.sleep(1)
            os.kill(os.getpid(), 9)

        threading.Thread(target=shutdown, daemon=True).start()
        return jsonify(status="ok", message="Server shutting down"), 200

    elif action == "enable":
        settings["server"]["enabled"] = True
        with open(SETTINGS_PATH, "w") as f:
            json.dump(settings, f, indent=2)

        log_server_event(action, user_name, email, user_uid, caller_ip, "success")
        return jsonify(status="ok", message="Enabled flag set. Restart the server to apply."), 200

    else:
        return jsonify(status="rejected", reason=f"Unknown action: {action}"), 400


# ── Gate control ────────────────────────────────────────────────────────────────
@app.route("/gate/control", methods=["POST"])
def control_gate():
    data = request.get_json(force=True)

    gate_number = data.get("gate")
    if not gate_number:
        return jsonify(status="rejected", reason="Missing gate field"), 400
    target_state = data["state"].lower()
    command_id = data.get("commandId") or str(uuid.uuid4())
    user_name = data.get("userName", "unknown")
    user_ip = request.remote_addr or "unknown"

    gate_key = gate_number.lower().replace(" ", "")
    ref = db.reference(f"/gates/{gate_key}")

    current = ref.get() or {}
    previous_state = current.get("state", "closed")

    if current.get("actionStatus") in ["Opening...", "Closing..."]:
        return jsonify(status="rejected", reason="gate busy", commandId=command_id), 409

    if current.get("lastCommandId") == command_id:
        return jsonify(status="acknowledged", duplicate=True, commandId=command_id), 200

    ref.update({
        "lastCommandId": command_id,
        "actionStatus": "Opening..." if target_state == "open" else "Closing...",
        "state": current.get("state", "closed"),
        "lastAckAt": int(time.time())
    })

    threading.Thread(
        target=run_gate_sequence,
        args=(gate_key, target_state, user_ip, user_name, command_id, previous_state),
        daemon=True
    ).start()

    return jsonify(status="acknowledged", commandId=command_id), 200


# ── List logging ────────────────────────────────────────────────────────────────
@app.route("/lists/log", methods=["POST"])
def log_list_addition():
    data = request.get_json(force=True)

    list_type = data.get("listType")
    if list_type not in ["whitelist", "blacklist"]:
        return jsonify(status="rejected", reason="Invalid listType"), 400

    item = data.get("item", {})
    if not item.get("name") or not item.get("reason"):
        return jsonify(status="rejected", reason="Missing name or reason"), 400

    user_name = data.get("userName", "unknown")
    user_ip = request.remote_addr or "unknown"
    command_id = data.get("commandId") or str(uuid.uuid4())

    now = datetime.now(timezone.utc)
    date_key = f"{now.day:02d}_{now.month:02d}_{now.year}"
    hour_minute = f"{now.hour:02d}:{now.minute:02d}"
    second = f"{now.second:02d}"

    log_entry = {
        "listType": list_type,
        "item": {
            "name": item.get("name"),
            "reason": item.get("reason"),
            "expiresAt": item.get("expiresAt", -1)
        },
        "user": user_name,
        "ip": user_ip,
        "timestamp": int(time.time()),
        "commandId": command_id
    }

    log_ref = db.reference(f"/list_logs/{date_key}/{hour_minute}")
    log_ref.child(f"S:{second}").set(log_entry)

    return jsonify(status="acknowledged", commandId=command_id), 200


# ── Gate sequence ───────────────────────────────────────────────────────────────
def run_gate_sequence(gate_key, target_state, user_ip, user_name, command_id, previous_state):
    ref = db.reference(f"/gates/{gate_key}")
    delay = settings["gates"].get("movementDelaySeconds", 2)

    # Print to terminal
    gate_name = gate_key.replace("gate", "Gate ")
    print(f"{gate_name} {'opening' if target_state == 'open' else 'closing'}")

    ref.update({"actionStatus": "Opening..." if target_state == "open" else "Closing..."})
    time.sleep(delay)

    ref.update({"state": target_state, "actionStatus": "Ready"})

    # print to terminal at end of action
    print(f"{gate_name} {'opened' if target_state == 'open' else 'closed'}")

    date_key = datetime.now(timezone.utc).strftime("%d_%m_%Y")
    hour_key = datetime.now(timezone.utc).strftime("%H:%M")

    log_ref = (
        db.reference("logs")
        .child(date_key)
        .child(hour_key)
        .child(datetime.now(timezone.utc).strftime("S:%S"))
    )

    log_ref.set({
        "gate": gate_key,
        "ip": user_ip,
        "user": user_name,
        "timestamp": int(time.time()),
        "from_to": f"{previous_state} -> {target_state}",
        "commandId": command_id,
        "action": target_state
    })

# ── Image upload ────────────────────────────────────────────────────────────────
@app.route("/chat/upload-image", methods=["POST"])
def upload_image():
    if not request.content_type or "image" not in request.content_type:
        return jsonify({"error": "invalid content type"}), 400

    data = request.data
    if not data:
        return jsonify({"error": "empty body"}), 400

    # 5MB hard limit — compressImage on the app side keeps it well under this
    if len(data) > 5 * 1024 * 1024:
        return jsonify({"error": "file too large"}), 413

    filename = f"{uuid.uuid4().hex}.jpg"
    path = os.path.join(UPLOAD_FOLDER, filename)

    with open(path, "wb") as f:
        f.write(data)

    # Build the URL the app can actually reach.
    # If host is 0.0.0.0 (listen on all interfaces), use the incoming
    # request's host so we return the real LAN IP the device connected to.
    host = settings["server"].get("host", "0.0.0.0")
    port = settings["server"].get("port", 8080)

    if host == "0.0.0.0":
        # request.host_url already contains the correct IP:port the client used
        image_url = request.host_url.rstrip("/") + "/media/" + filename
    else:
        image_url = f"http://{host}:{port}/media/{filename}"

    print(f"[upload] saved {filename} ({len(data)} bytes) -> {image_url}")

    return jsonify({"imageUrl": image_url})



@app.route("/media/<filename>")
def media(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# ── Entry point ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    srv = settings["server"]
    if not srv.get("enabled", True):
        print("Server is disabled in settings.json. Set enabled=true to start.")
        sys.exit(0)

    app.run(
        host=srv.get("host", "0.0.0.0"),
        port=srv.get("port", 8080),
        debug=srv.get("debug", False)
    )