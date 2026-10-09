import importlib
import io

from PIL import Image

from conftest import add_formal, add_user
from swaps.db import connect


def _png(size=(3000, 2000)):
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, "PNG")
    buf.seek(0)
    return buf


def _client(db_path, monkeypatch):
    c = connect(db_path)
    add_user(c, 1)
    add_formal(c, 1, slots=5)
    c.execute("INSERT INTO allocations(user_id, formal_id, status) VALUES (1,1,'active')")
    c.commit(); c.close()
    monkeypatch.setenv("DB_PATH", db_path); monkeypatch.setenv("COOKIE_SECURE", "0")
    from swaps import config as cfg
    importlib.reload(cfg)
    from swaps import create_app
    app = create_app(); app.config["TESTING"] = True
    a = app.test_client()
    with a.session_transaction() as s:
        s["is_admin"] = True; s["_csrf"] = "tok"
    return a


def test_email_all_with_photos(db_path, monkeypatch):
    a = _client(db_path, monkeypatch)
    sent = []
    import threading
    from swaps import emailer
    monkeypatch.setattr(emailer, "send", lambda *a, **k: sent.append((a, k)) or True)
    monkeypatch.setattr(threading, "Thread",
                        lambda target, daemon: type("T", (), {"start": lambda s: target()})())
    assert b'name="photos"' in a.get("/admin/formals/1/roster").data
    r = a.post("/admin/formals/1/email-all", content_type="multipart/form-data", data={
        "_csrf": "tok", "subject": "Meeting point", "body": "See map",
        "photos": [(_png(), "map.png"), (_png((100, 100)), "b.png")]})
    assert r.status_code == 302
    assert len(sent) == 1
    (to, subject, html), kw = sent[0]
    atts = kw["attachments"]
    assert [x[2] for x in atts] == ["photo1", "photo2"]
    assert 'src="cid:photo1"' in html and 'src="cid:photo2"' in html
    big = Image.open(io.BytesIO(atts[0][1]))
    assert big.format == "JPEG" and max(big.size) == 1600


def test_email_all_rejects_non_image(db_path, monkeypatch):
    a = _client(db_path, monkeypatch)
    r = a.post("/admin/formals/1/email-all", content_type="multipart/form-data", data={
        "_csrf": "tok", "subject": "x", "body": "y",
        "photos": [(io.BytesIO(b"not an image"), "x.png")]})
    assert r.status_code == 302
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM email_log").fetchone()[0] == 0
    c.close()


def test_send_passes_content_id_to_resend(monkeypatch):
    from swaps import config, emailer
    monkeypatch.setattr(config, "EMAIL_MODE", "live")
    monkeypatch.setattr(emailer, "_record", lambda *a, **k: None)
    seen = {}

    class R:
        status_code = 200; text = ""

    monkeypatch.setattr(emailer.requests, "post",
                        lambda url, json, headers, timeout: seen.update(json) or R())
    assert emailer.send("a@b.c", "s", "<p>h</p>",
                        attachments=[("p.jpg", b"x", "photo1"), ("c.ics", b"y")])
    assert seen["attachments"][0]["content_id"] == "photo1"
    assert "content_id" not in seen["attachments"][1]
