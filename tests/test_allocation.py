import random
from collections import Counter, defaultdict

from swaps.allocation import run_ballot


def places(assignments):
    c = Counter()
    for uid, _fid, _r in assignments:
        c[uid] += 1
    return c


def test_everyone_gets_top_choice_when_space():
    formals = {1: 5, 2: 5}
    prefs = {10: [1, 2], 11: [2, 1], 12: [1]}
    a, _ = run_ballot(formals, prefs, "seed")
    round1 = {(u, f) for u, f, r in a if r == 1}
    assert (10, 1) in round1 and (11, 2) in round1 and (12, 1) in round1


def test_oversubscribed_lottery_and_cascade():
    # 3 people want A (cap 2); loser must fall through to B in the SAME round.
    formals = {1: 2, 2: 5}
    prefs = {u: [1, 2] for u in (10, 11, 12)}
    a, log = run_ballot(formals, prefs, "s1")
    r1 = [(u, f) for u, f, r in a if r == 1]
    assert sorted(f for _, f in r1).count(1) == 2
    assert sorted(f for _, f in r1).count(2) == 1
    assert any("oversubscribed" in line for line in log)


def test_determinism_same_seed():
    formals = {i: 3 for i in range(1, 6)}
    prefs = {u: random.Random(u).sample(range(1, 6), 4) for u in range(100, 140)}
    a1, l1 = run_ballot(formals, prefs, "fixed-seed")
    a2, l2 = run_ballot(formals, prefs, "fixed-seed")
    assert a1 == a2 and l1 == l2
    a3, _ = run_ballot(formals, prefs, "other-seed")
    assert a1 != a3  # overwhelmingly likely with 40 users, 15 seats


def test_no_second_place_before_everyone_satisfiable_has_one():
    # A(2 seats), B(2 seats); 3 applicants all ranking [A, B]:
    # round 1 must give each of the 3 exactly one place; the leftover seat
    # is a round-2 second place.
    formals = {1: 2, 2: 2}
    prefs = {u: [1, 2] for u in (10, 11, 12)}
    a, _ = run_ballot(formals, prefs, "s2")
    by_round = defaultdict(list)
    for u, f, r in a:
        by_round[r].append(u)
    assert sorted(by_round[1]) == [10, 11, 12]      # everyone seated once first
    assert len(by_round[2]) == 1                     # then one second place
    assert places(a).most_common(1)[0][1] == 2


def test_unlucky_user_only_ranked_full_formals():
    # A has 1 seat, three people rank only A; B has spare that only u3 ranked.
    # Exactly one of u1-u3 gets A; the others (who ranked nothing else) get
    # nothing, and seconds at B may still be handed out afterwards.
    formals = {1: 1, 2: 2}
    prefs = {1: [1], 2: [1], 3: [1, 2]}
    a, _ = run_ballot(formals, prefs, "s3")
    p = places(a)
    a_winners = [u for u, f, _ in a if f == 1]
    assert len(a_winners) == 1
    for u in (1, 2):
        if u not in a_winners:
            assert p[u] == 0  # ranked only the full formal -> nothing, by design
    assert p[3] >= 1  # u3 always satisfiable via B


def test_round_and_capacity_invariants_random_instances():
    rng = random.Random(42)
    for case in range(30):
        n_formals = rng.randint(1, 6)
        formals = {f: rng.randint(1, 4) for f in range(1, n_formals + 1)}
        prefs = {}
        for u in range(1, rng.randint(2, 25)):
            k = rng.randint(1, n_formals)
            prefs[u] = rng.sample(list(formals), k)
        a, _ = run_ballot(formals, prefs, f"case-{case}")
        again, _ = run_ballot(formals, prefs, f"case-{case}")
        assert a == again, "same seed must reproduce identical results"

        seats = Counter(f for _, f, _ in a)
        for f, n in seats.items():
            assert n <= formals[f], "formal over capacity"
        assert len({(u, f) for u, f, _ in a}) == len(a), "duplicate seat at same formal"

        per_round = Counter((u, r) for u, _, r in a)
        assert all(n == 1 for n in per_round.values()), "two places in one round"
        rounds_of = defaultdict(set)
        for u, _, r in a:
            rounds_of[u].add(r)
        for u, rs in rounds_of.items():
            assert rs == set(range(1, len(rs) + 1)), \
                "a round-N place requires a place in every earlier round"

        # Anyone with zero places must have every ranked formal full.
        remaining = dict(formals)
        for _, f, _ in a:
            remaining[f] -= 1
        got = {u for u, _, _ in a}
        for u, plist in prefs.items():
            if u not in got:
                assert all(remaining[f] == 0 for f in plist), \
                    "user left empty-handed despite a satisfiable preference"
