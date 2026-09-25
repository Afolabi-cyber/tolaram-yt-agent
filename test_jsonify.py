from server import app
from data_store import compute_dashboard_stats
from flask import jsonify
import traceback

with app.app_context():
    try:
        stats = compute_dashboard_stats()
        print("Data computed successfully")
        resp = jsonify({"ok": True, "data": stats})
        print("JSONIFY SUCCEEDED")
    except Exception as e:
        print("ERROR IN JSONIFY:")
        traceback.print_exc()
