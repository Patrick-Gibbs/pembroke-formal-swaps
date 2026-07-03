"""Production entrypoint: waitress on localhost, single process, plus the
slot-release worker thread."""
from waitress import serve

from swaps import create_app, config
from swaps.db import init_db
from swaps import release_worker

init_db()
app = create_app()

if __name__ == "__main__":
    release_worker.start()
    print(f"Serving on 127.0.0.1:{config.LISTEN_PORT} (EMAIL_MODE={config.EMAIL_MODE})",
          flush=True)
    serve(app, host="127.0.0.1", port=config.LISTEN_PORT, threads=8)
