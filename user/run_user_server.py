"""Savi Nutrition User Server — runs on port 5001."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app

if __name__ == '__main__':
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    host = os.environ.get("HOST", "0.0.0.0")
    app.run(host=host, port=5001, debug=debug, threaded=True,
            use_reloader=False)
