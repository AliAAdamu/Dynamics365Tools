"""D365 DMF Tester — entry point.  Run with: python app.py"""
import os
import secrets

from flask import Flask

import routes  # flat module — ensures it is importable before blueprint registration


def create_app() -> Flask:
    base_dir = os.path.dirname(os.path.abspath(__file__))

    app = Flask(
        __name__,
        template_folder=os.path.join(base_dir, "templates"),
        static_folder=os.path.join(base_dir, "static"),
    )

    # Persistent secret key so sessions survive restarts
    key_path = os.path.join(base_dir, "data", ".flask_secret")
    os.makedirs(os.path.join(base_dir, "data"), exist_ok=True)
    if not os.path.exists(key_path):
        with open(key_path, "w") as fh:
            fh.write(secrets.token_hex(32))
    with open(key_path) as fh:
        app.secret_key = fh.read().strip()

    # Custom Jinja2 filters
    @app.template_filter("basename")
    def _basename(path: str) -> str:
        import os
        return os.path.basename(path) if path else ""

    app.register_blueprint(routes.bp)
    return app


application = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    application.run(host="127.0.0.1", port=port, debug=False, threaded=True)
