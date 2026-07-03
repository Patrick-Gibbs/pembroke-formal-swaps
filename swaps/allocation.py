"""Preference-honouring random ballot with fairness rounds and group blocks.

Applicants are "units": a solo member (size 1) or a ballot group (size = number
of accepted members). A unit is allocated as a block — all of its members get
the formal, or none do.

Exact policy implemented (see README "Allocation policy"):

1. Allocation runs in ROUNDS. Within a round each unit can gain at most ONE
   formal. Round N+1 (extra places) starts only when round N assigns
   everything it can, so no unit receives a 2nd formal while some unit that
   still has a satisfiable preference has none.
2. Within a round, units repeatedly bid on their FIRST VIABLE choice: the
   highest-ranked formal on their list with at least `size` free seats that
   they don't already hold. All bids at the same step are resolved together
   (everyone's top choice is considered before anyone's second).
3. If a formal can seat every bidder, all get in. Otherwise bidders are drawn
   in a SEEDED random order and admitted while they still fit — a group that
   doesn't fit is passed over even if a smaller unit drawn later still fits
   (the leftover seats stay claimable by smaller units, and the passed-over
   group falls through to its next choice at the next step).
4. A unit drops out of a round when it holds a place or has no viable choice
   left. Ending up with nothing therefore means: every formal you ranked
   filled up (or never had enough adjacent seats for your group size).
5. Same seed + same data reproduces the run exactly; every draw is logged.
"""
import random
from collections import defaultdict


def run_ballot(formals, preferences, seed, sizes=None):
    """Pure, deterministic allocation.

    formals: {formal_id: free_seats}
    preferences: {unit_id: [formal_id, ...] in rank order}
    sizes: {unit_id: seats_needed}, default 1 each
    seed: str/int recorded by the caller.

    Returns (assignments, log): assignments is [(unit_id, formal_id, round_no)]
    in assignment order; log describes each random draw.
    """
    rng = random.Random(str(seed))
    sizes = sizes or {}

    def size(u):
        return max(1, int(sizes.get(u, 1)))

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
            bids = defaultdict(list)  # formal_id -> [unit_id] in deterministic order
            for uid in prefs:
                if uid in resolved or uid in exhausted:
                    continue
                pick = next((f for f in prefs[uid]
                             if remaining[f] >= size(uid) and f not in holdings[uid]),
                            None)
                if pick is None:
                    exhausted.add(uid)
                else:
                    bids[pick].append(uid)
            if not bids:
                break

            for fid in sorted(bids):
                units = bids[fid]
                demand = sum(size(u) for u in units)
                if demand <= remaining[fid]:
                    winners = units
                else:
                    order = list(units)
                    rng.shuffle(order)
                    winners, seats_left = [], remaining[fid]
                    for u in order:
                        if size(u) <= seats_left:
                            winners.append(u)
                            seats_left -= size(u)
                    log.append(
                        f"round {round_no}: formal {fid} oversubscribed "
                        f"({demand} seats wanted by {len(units)} unit(s), "
                        f"{remaining[fid]} free) -> admitted {sorted(winners)}, "
                        f"passed over {sorted(u for u in units if u not in winners)}")
                for u in winners:
                    remaining[fid] -= size(u)
                    holdings[u].add(fid)
                    resolved.add(u)
                    assignments.append((u, fid, round_no))
                    progress = True

        if not progress:
            break

    return assignments, log
