from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, db
import time
import threading
import uuid

cred = credentials.Certificate(r"C:\Users\y61\PycharmProjects\s\serviceKey.json")
firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://mainproj-5e50a-default-rtdb.europe-west1.firebasedatabase.app/'
})

app = Flask(__name__)

@app.route("/gate/control", methods=["POST"])
def control_gate():
    data = request.get_json(force=True)

    gate_number = data["gate"]
    target_state = data["state"]
    command_id = data.get("commandId")

    gate_key = gate_number.lower().replace(" ", "")
    ref = db.reference(f"/gates/{gate_key}")

    current = ref.get() or {}

    # ---- BUSY REJECTION ----
    if current.get("actionStatus", "") in ["Opening...", "Closing..."]:
        return jsonify(
            status="rejected",
            reason="gate busy"
        ), 409

    # ---- IDEMPOTENCY ----
    if current.get("lastCommandId") == command_id:
        return jsonify(
            status="acknowledged",
            duplicate=True
        ), 200

    # ---- ACK ----
    ref.update({
        "lastCommandId": command_id,
        "lastAckAt": int(time.time())
    })

    threading.Thread(
        target=run_gate_sequence,
        args=(gate_key, target_state),
        daemon=True
    ).start()

    return jsonify(
        status="acknowledged",
        commandId=command_id
    ), 200


def run_gate_sequence(gate_key, target_state):
    ref = db.reference(f"/gates/{gate_key}")

    if target_state == "open":
        ref.update({"actionStatus": "Opening..."})
        time.sleep(2)

        ref.update({"state": "open", "actionStatus": "Opened"})
        time.sleep(26)

        ref.update({"actionStatus": "Closing..."})
        time.sleep(2)

        ref.update({"state": "closed", "actionStatus": "Closed"})
        time.sleep(1)

        ref.update({"actionStatus": "Ready"})

    else:
        ref.update({"actionStatus": "Closing..."})
        time.sleep(2)

        ref.update({"state": "closed", "actionStatus": "Closed"})
        time.sleep(1)

        ref.update({"actionStatus": "Ready"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
