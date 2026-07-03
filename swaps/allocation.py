"""Preference-honouring random ballot with fairness rounds.

Exact policy implemented (surface this to the admin before relying on edge
cases — see README "Allocation policy"):

1. Allocation runs in ROUNDS. Within a round each applicant can gain at most
   ONE place. Round N+1 (extra places) starts only when round N assigns
   everything it can, so nobody receives a 2nd place while someone who still
   has a satisfiable preference has none.
2. Within a round, applicants repeatedly bid on their FIRST VIABLE choice:
   the highest-ranked formal on their list that still has free seats and
   that they don't already hold a place at. All bids at the same step are
   resolved together (everyone's top choice is considered before anyone's
   second, because losing a bid means that formal just filled, pushing the
   loser to their next viable choice at the next step).
3. If a formal has enough seats for all bidders, everyone gets in. Otherwise
   winners are drawn uniformly at random with a SEEDED RNG: the same seed and
   the same data reproduce the run exactly. Every lottery is logged.
4. An applicant drops out of a round when they hold a place or have no
   viable choice left (all remaining ranked formals full). That is the only
   way to end up with nothing while others get seconds: every formal you
   ranked filled up before your turn.
"""
import random
from collections import defaultdict


def run_ballot(formals, preferences, seed):
    """Pure, deterministic allocation.

    formals: {formal_id: free_seats}
    preferences: {user_id: [formal_id, ...] in rank order}
    seed: str/int recorded by the caller; same seed + inputs => same result.

    Returns (assignments, log): assignments is [(user_id, formal_id, round_no)]
    in assignment order; log is human-readable lines describing each lottery.
    """
    rng = random.Random(str(seed))
    remaining = {fid: max(0, int(cap)) for fid, cap in formals.items()}
    prefs = {uid: [f for f in plist if f in remaining]
             for uid, plist in sorted(preferences.items())}
    holdings = defaultdict(set)
    assignments, log = [], []
    round_no = 0

    while True:
        round_no += 1
        resolved, exhausted = set(), set()
        progress = False

        while True:
            bids = defaultdict(list)  # formal_id -> [user_id], uid order deterministic
            for uid in prefs:
                if uid in resolved or uid in exhausted:
                    continue
                pick = next((f for f in prefs[uid]
                             if remaining[f] > 0 and f not in holdings[uid]), None)
                if pick is None:
                    exhausted.add(uid)
                else:
                    bids[pick].append(uid)
            if not bids:
                break

            for fid in sorted(bids):
                users = bids[fid]
                cap = remaining[fid]
                if cap >= len(users):
                    winners = users
                else:
                    winners = sorted(rng.sample(users, cap))
                    log.append(
                        f"round {round_no}: formal {fid} oversubscribed "
                        f"({len(users)} bidders, {cap} seats) -> winners {winners}, "
                        f"passed over {[u for u in users if u not in winners]}")
                for uid in winners:
                    remaining[fid] -= 1
                    holdings[uid].add(fid)
                    resolved.add(uid)
                    assignments.append((uid, fid, round_no))
                    progress = True

        if not progress:
            break

    return assignments, log
