import importlib

from conftest import add_formal, add_user
from swaps.db import connect


def test_delete_formal_with_reviews_and_swaps(db_path, monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", db_path); monkeypatch.setenv("COOKIE_SECURE", "0")
    from swaps import config as cfg
    importlib.reload(cfg)
    monkeypatch.setattr(cfg, "PHOTOS_DIR", str(tmp_path))
    (tmp_path / "p.jpg").write_bytes(b"x")
    c = connect(db_path)
    add_user(c, 1); add_user(c, 2)
    add_formal(c, 1, slots=3); add_formal(c, 2, slots=3)
    c.execute("INSERT INTO allocations(user_id, formal_id, status) VALUES (1,1,'cancelled')")
    c.execute("INSERT INTO released_slots(formal_id, allocation_id, release_at) "
              "VALUES (1, 1, '2000-01-01 00:00:00')")
    rid = c.execute("INSERT INTO reviews(user_id, formal_id, course_stars, vibe_stars) "
                    "VALUES (1,1,3,2)").lastrowid
    c.execute("INSERT INTO review_photos(review_id, filename) VALUES (?, 'p.jpg')", (rid,))
    c.execute("INSERT INTO swap_requests(from_user, from_formal, to_user, to_formal) "
              "VALUES (1,1,2,2)")
    c.close()
    from swaps import create_app
    app = create_app(); app.config["TESTING"] = True
    adm = app.test_client()
    with adm.session_transaction() as s:
        s["is_admin"] = True; s["_csrf"] = "tok"
    r = adm.post("/admin/formals/1/delete", data={"_csrf": "tok"}, follow_redirects=True)
    assert r.status_code == 200 and "Deleted." in r.data.decode()
    c = connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM formals WHERE id=1").fetchone()[0] == 0
    for t in ("reviews", "review_photos", "swap_requests", "allocations", "released_slots"):
        assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t
    assert c.execute("SELECT COUNT(*) FROM formals WHERE id=2").fetchone()[0] == 1
    assert not (tmp_path / "p.jpg").exists()
