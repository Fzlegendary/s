from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, db
from datetime import datetime, timezone
import time
import threading
import uuid

# Firebase credentials
cred = credentials.Certificate(
    r"C:\Users\y61\PycharmProjects\s\serviceKey.json"
)

firebase_admin.initialize_app(cred, {
    "databaseURL": "https://mainproj-5e50a-default-rtdb.europe-west1.firebasedatabase.app/"
})

app = Flask(__name__)


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

    # Get current state BEFORE updating
    current = ref.get() or {}
    previous_state = current.get("state", "closed")

    # ---- BUSY CHECK ----
    if current.get("actionStatus") in ["Opening...", "Closing..."]:
        return jsonify(
            status="rejected",
            reason="gate busy",
            commandId=command_id
        ), 409

    # ---- IDEMPOTENCY ----
    if current.get("lastCommandId") == command_id:
        return jsonify(
            status="acknowledged",
            duplicate=True,
            commandId=command_id
        ), 200

    # ---- ACK + MARK BUSY ----
    ref.update({
        "lastCommandId": command_id,
        "actionStatus": "Opening..." if target_state == "open" else "Closing...",
        "state": current.get("state", "closed"),
        "lastAckAt": int(time.time())
    })

    # Start background sequence
    threading.Thread(
        target=run_gate_sequence,
        args=(gate_key, target_state, user_ip, user_name, command_id, previous_state),
        daemon=True
    ).start()

    return jsonify(
        status="acknowledged",
        commandId=command_id
    ), 200


def run_gate_sequence(gate_key, target_state, user_ip, user_name, command_id, previous_state):
    ref = db.reference(f"/gates/{gate_key}")

    # Set intermediate state
    ref.update({"actionStatus": "Opening..." if target_state == "open" else "Closing..."})
    time.sleep(2)  # simulate gate movement

    # Update final state
    ref.update({
        "state": target_state,
        "actionStatus": "Ready"
    })

    # Date bucket (UTC, stable, sortable)
    date_key = datetime.now(timezone.utc).strftime("%d_%m_%Y")
    hour_key = datetime.now(timezone.utc).strftime("%H:%M")

    # Reference: logs -> date -> auto-id
    log_ref = db.reference("logs").child(date_key).child(hour_key).push({
        "gate": gate_key,
        "ip": user_ip,
        "user": user_name,
        "timestamp": int(time.time()),
        "from_to": f"{previous_state} -> {target_state}",
        "commandId": command_id,
        "action": target_state
    })
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True, ssl_context=('cert.pem', 'key.pem'))
