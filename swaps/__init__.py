import logging

from flask import Flask, session

from . import config
from .db import close_db, get_db, get_setting
from .security import check_csrf, csrf_token, current_user

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")


def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")
    if not config.SECRET_KEY:
        raise RuntimeError("SECRET_KEY missing — create .env from .env.example")
    app.secret_key = config.SECRET_KEY
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=config.COOKIE_SECURE,
        SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=64 * 1024,
    )

    from .views.auth import bp as auth_bp
    from .views.main import bp as main_bp
    from .views.admin import bp as admin_bp
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp)

    app.before_request(check_csrf)
    app.teardown_appcontext(close_db)

    @app.context_processor
    def inject():
        return {
            "csrf_token": csrf_token,
            "user": current_user(),
            "is_admin": bool(session.get("is_admin")),
            "current_term": get_setting(get_db(), "current_term"),
        }

    return app
