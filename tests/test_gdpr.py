from conftest import add_formal, add_user
from swaps.services import delete_user_data, export_user_data


def _setup(db):
    for u in (1, 2):
        add_user(db, u)
    add_formal(db, 1, slots=5, term="T1")
    db.execute("UPDATE formals SET status='open'")
    db.execute("INSERT INTO preferences(user_id, formal_id, rank, term) VALUES (1,1,1,'T1')")
    db.execute("INSERT INTO ballot_caps(user_id, term, max_places) VALUES (1,'T1',2)")
    db.execute("INSERT INTO allocations(user_id, formal_id, status, source) "
               "VALUES (1,1,'active','ballot')")
    db.execute("INSERT INTO subscriptions(user_id, formal_id) VALUES (1,1)")
    db.execute("INSERT INTO reviews(user_id, formal_id, course_stars, vibe_stars, review) "
               "VALUES (1,1,3,2,'great')")
    rid = db.execute("SELECT id FROM reviews WHERE user_id=1").fetchone()["id"]
    db.execute("INSERT INTO review_photos(review_id, filename) VALUES (?, 'p.jpg')", (rid,))
    # user 1 leads a group with user 2 as a member
    db.execute("INSERT INTO ballot_groups(id, term, leader_user_id) VALUES (1,'T1',1)")
    for u in (1, 2):
        db.execute("INSERT INTO ballot_group_members(group_id, user_id, status) "
                   "VALUES (1, ?, 'accepted')", (u,))


def test_export_includes_everything(db):
    _setup(db)
    d = export_user_data(db, 1)
    assert d["email"] == "u1@pem.cam.ac.uk"
    assert len(d["preferences"]) == 1 and d["preferences"][0]["rank"] == 1
    assert d["max_places_caps"][0]["max_places"] == 2
    assert len(d["allocations"]) == 1
    assert len(d["reviews"]) == 1 and d["reviews"][0]["review"] == "great"
    assert d["group_memberships"][0]["is_leader"] == 1


def test_delete_erases_and_returns_photos(db):
    _setup(db)
    photos = delete_user_data(db, 1)
    assert "p.jpg" in photos
    assert db.execute("SELECT COUNT(*) n FROM users WHERE id=1").fetchone()["n"] == 0
    for t in ("preferences", "ballot_caps", "allocations", "subscriptions",
              "reviews", "review_photos"):
        n = db.execute(f"SELECT COUNT(*) n FROM {t} "
                       + ("WHERE user_id=1" if t != "review_photos" else "")).fetchone()["n"]
        assert n == 0, t
    # the group the user led is disbanded (member rows + group gone)
    assert db.execute("SELECT COUNT(*) n FROM ballot_groups").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) n FROM ballot_group_members").fetchone()["n"] == 0
    # the other user is untouched
    assert db.execute("SELECT COUNT(*) n FROM users WHERE id=2").fetchone()["n"] == 1


def test_delete_nulls_released_slot_refs(db):
    add_user(db, 1)
    add_formal(db, 1, slots=1, term="T1")
    db.execute("INSERT INTO allocations(id, user_id, formal_id, status, source) "
               "VALUES (7,1,1,'cancelled','ballot')")
    db.execute("INSERT INTO released_slots(formal_id, allocation_id, release_at, "
               "claimed_by) VALUES (1, 7, '2030-01-01 00:00:00', 1)")
    delete_user_data(db, 1)
    row = db.execute("SELECT claimed_by, allocation_id FROM released_slots").fetchone()
    assert row["claimed_by"] is None and row["allocation_id"] is None
