"""Production entry point for the SAVI Nutrition Inventory System.

Run with:  python run_server.py
Serves on http://localhost:5000
"""
import os
import app

if __name__ == "__main__":
    try:
        from app import start_backup_scheduler
        start_backup_scheduler()
    except Exception:
        pass
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.app.run(host=host, port=port, debug=debug, threaded=True,
                use_reloader=False)