from flask import Flask, request, jsonify
import firebase_admin
from firebase_admin import credentials, db
from queue import Queue

# -------------------------------
# Firebase setup
# -------------------------------
cred = credentials.Certificate(r"C:\Users\y61\PycharmProjects\s\serviceKey.json")
firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://mainproj-5e50a-default-rtdb.europe-west1.firebasedatabase.app/'
})

# -------------------------------
# Flask app setup
# -------------------------------
app = Flask(__name__)

# Simple in-memory queue
task_queue = Queue()


# -------------------------------
# Middleware: log all requests
# -------------------------------
@app.before_request
def log_request():
    data = request.get_json(silent=True)
    print("Request received:")
    print("Method:", request.method)
    print("Path:", request.path)
    print("JSON payload:", data)


# -------------------------------
# Helper function: log message to Firebase
# -------------------------------
def log_to_firebase(message):
    ref = db.reference('/logs')
    ref.push({"message": message})
    print(f"Logged to Firebase: {message}")


# -------------------------------
# Helper function: update data in Firebase
# -------------------------------
def update_firebase(path, data):
    ref = db.reference(path)
    ref.update(data)
    print(f"Updated Firebase at {path}: {data}")



def log_queue(queue):
    # Convert the Queue object to a list
    queue_list = list(queue.queue)  # 'queue.queue' is the underlying deque
    ref = db.reference('/queue')
    ref.push({"queue": queue_list})
    print(f"Logged queue to Firebase: {queue_list}")

# -------------------------------
# Helper function: add item to queue
# -------------------------------
def add_to_queue(item):
    task_queue.put(item)
    print(f"Added to queue: {item}")
    print("logging queue...")
    log_queue(task_queue)



# -------------------------------
# Existing greet route
# -------------------------------
@app.route("/greet", methods=["POST"])
def greet():
    data = request.get_json(force=True)
    name = data.get("name", "World")

    # Example workflow
    log_to_firebase(f"Greeting request for {name}")
    update_firebase("/latest_greeting", {"name": name})
    add_to_queue({"action": "greet", "name": name})

    return jsonify(message=f"Hello {name}!")


# -------------------------------
# Example route to view queue contents
# -------------------------------
@app.route("/queue", methods=["GET"])
def view_queue():
    items = list(task_queue.queue)
    return jsonify(queue=items)

@app.route("/log-message", methods=["POST"])
def log_message():
    data = request.get_json(force=True)
    log_to_firebase(data["message"])
# -------------------------------
# Run server
# -------------------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8080)
