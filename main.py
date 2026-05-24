from flask import Flask, request, jsonify
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
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), "Desktop", "ServerSettings", "settings.json")
with open(SETTINGS_PATH) as f:
    settings = json.load(f)

# ── Firebase credentials ────────────────────────────────────────────────────────
cred = credentials.Certificate(settings["firebase"]["serviceKeyPath"])
firebase_admin.initialize_app(cred, {
    "databaseURL": settings["firebase"]["databaseURL"]
})

app = Flask(__name__)


# ── Remote server control ───────────────────────────────────────────────────────
@app.route("/server/control", methods=["POST"])
def server_control():
    """
    Remote control endpoint for Android app.
    Expected JSON payload:
    {
        "action": "disable" | "enable" | "status",
        "shutdownKey": "<your secret key from settings.json>"
    }
    """
    rc = settings.get("remoteControl", {})

    # Check if remote control is allowed
    if not rc.get("allowRemoteDisable", False):
        return jsonify(status="rejected", reason="Remote control is disabled"), 403

    # IP whitelist check (if list is non-empty)
    allowed_ips = rc.get("allowedRemoteIPs", [])
    caller_ip = request.remote_addr
    if allowed_ips and caller_ip not in allowed_ips:
        return jsonify(status="rejected", reason="IP not allowed", ip=caller_ip), 403

    data = request.get_json(force=True)
    action = data.get("action", "").lower()
    provided_key = data.get("shutdownKey", "")
    expected_key = settings["server"].get("shutdownKey", "")

    # Validate secret key
    if provided_key != expected_key:
        return jsonify(status="rejected", reason="Invalid shutdown key"), 401

    if action == "status":
        return jsonify(
            status="ok",
            serverEnabled=settings["server"].get("enabled", True),
            host=settings["server"].get("host"),
            port=settings["server"].get("port"),
            uptime=int(time.time())
        ), 200

    elif action == "disable":
        if not settings["server"].get("enabled", True):
            return jsonify(status="ok", message="Server already disabled"), 200

        # Update settings file
        settings["server"]["enabled"] = False
        with open(SETTINGS_PATH, "w") as f:
            json.dump(settings, f, indent=2)

        # Shutdown Flask in a background thread (gives time to send response)
        def shutdown():
            time.sleep(1)
            os.kill(os.getpid(), 9)

        threading.Thread(target=shutdown, daemon=True).start()
        return jsonify(status="ok", message="Server shutting down"), 200

    elif action == "enable":
        # Mark enabled in settings (server must be restarted manually after this)
        settings["server"]["enabled"] = True
        with open(SETTINGS_PATH, "w") as f:
            json.dump(settings, f, indent=2)
        return jsonify(status="ok", message="Enabled flag set. Restart the server to apply."), 200

    else:
        return jsonify(status="rejected", reason=f"Unknown action: {action}"), 400


# ── Gate control ────────────────────────────────────────────────────────────────
@app.route("/gate/control", methods=["POST"])
def control_gate():
    data = request.get_json(force=True)

    gate_number = data["gate"]
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

    ref.update({"actionStatus": "Opening..." if target_state == "open" else "Closing..."})
    time.sleep(delay)

    ref.update({"state": target_state, "actionStatus": "Ready"})

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