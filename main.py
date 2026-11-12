#!/usr/bin/env python3
"""Deterministic rank-diffusion forecast for the Roblox Up-and-Coming top 10.

Reads the chart history from $MAKEFUN_INPUT_DIR (default /input) and writes
one static forecast to $MAKEFUN_OUTPUT_DIR (default /output).

Model: each game on the last chart keeps its last rank, drifts with an
exponentially weighted recent velocity that saturates over a 6-hour
timescale, and spreads with a diffusion width fitted from the history
itself.  That Normal diffusion is blended (weight 0.65) with an EMPIRICAL
transition kernel (weight 0.35) built from the history's own reads: for
every pair of reads up to 72 hours apart, the kernel records where a game
at a given rank bucket ended up, split by chart age (fresh, first seen
under 72 hours ago, versus older).  The kernel captures what a single
Normal cannot: rank-dependent drift (top games fall back, deep games climb
on average), rank-dependent spread, the fat tail of viral climbs, and the
newcomer boost (games fresh on the chart reach the top 10 far more often).
Per-place probabilities are averaged over the 72 forecast hours, then
sharpened with a temperature of 1.15 (the mid-range was slightly
overconfident in calibration), and the best candidates at each place are
rescaled to sum to 0.75.  Games age out of the chart 28 days after
creation when that date is known.
"""

import json
import math
import os
from datetime import datetime, timedelta

# v3 = v2 with two changes, validated on the public chart archive:
#   VEL_CLAMP 1.5 -> 2.5 (lets strong climbers drift further) and
#   TEMP 1.15 (sharpens the per-place averages).
# Practice window -34.95 vs v2 -35.23; 20 rolling 72h hold-out windows
# mean -37.14 vs v2 -37.23 (wins 13 of 20); 33-window mean -37.79 vs
# -37.88; 6-cut mean -38.79 vs -38.98.  v2 itself: mean -37.23 vs -37.63
# for the pure-Normal v1 (wins 15 of 20); practice -35.23 vs v1 -35.88.
ALPHA = 0.35          # empirical-kernel blend weight
TEMP = 1.15           # temperature on per-place averages (>1 sharpens)
NAMED = 0.75          # probability mass named per place
AGE_CUT = 72.0        # hours: fresh vs old chart age
SIGMA_MAX = 1.0
SIGMA_MIN = 0.25
SPREAD_CAP = 14.0
VEL_TAU = 6.0         # velocity saturation timescale, hours
VEL_CLAMP = 2.5       # ranks per hour
VEL_HALFLIFE = 24.0   # velocity recency half-life, hours
CAP = 30              # candidates named per place
FLOOR = 0.003         # minimum average probability to name

RANK_BUCKETS = [(1, 5), (6, 10), (11, 20), (21, 30), (31, 50), (51, 200)]
HOUR_BUCKETS = [(1, 6), (7, 18), (19, 36), (37, 60), (61, 72)]


def parse_time(text):
    """Parse an ISO 8601 timestamp ending in Z into an aware datetime."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def load_task(input_dir):
    try:
        with open(os.path.join(input_dir, "task.json"), encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}


def load_reads(input_dir):
    reads = []
    with open(os.path.join(input_dir, "history.ndjson"), encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                reads.append(json.loads(line))
    reads.sort(key=lambda rec: parse_time(rec["at"]))
    return reads


def norm_cdf(x):
    """Standard Normal CDF via math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def median(values):
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return 0.5 * (ordered[middle - 1] + ordered[middle])


def rank_bucket(rank):
    for index, (low, high) in enumerate(RANK_BUCKETS):
        if low <= rank <= high:
            return index
    return len(RANK_BUCKETS) - 1


def hour_bucket(hour):
    for index, (low, high) in enumerate(HOUR_BUCKETS):
        if low <= hour <= high:
            return index
    return len(HOUR_BUCKETS) - 1


def main():
    input_dir = os.environ.get("MAKEFUN_INPUT_DIR", "/input")
    output_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    task = load_task(input_dir)
    places = int(task.get("places", 10))
    window = task.get("window") or {}
    slots = int(window.get("slots", 72))
    output_name = task.get("output", "forecast.json")

    reads = load_reads(input_dir)
    last_time = parse_time(reads[-1]["at"])

    # Per-game series of (timestamp, rank), in time order, plus creation
    # times and the first time each game appeared on the chart.
    series = {}
    created = {}
    first_seen = {}
    for rec in reads:
        when = parse_time(rec["at"])
        for row in rec.get("up_and_coming") or []:
            uid = int(row["universe_id"])
            series.setdefault(uid, []).append((when, int(row["rank"])))
            if uid not in first_seen:
                first_seen[uid] = when
        details = rec.get("details") or {}
        for key in sorted(details):
            info = details[key] or {}
            stamp = info.get("created")
            if not stamp:
                continue
            uid = int(key)
            if uid in created:
                continue
            try:
                created[uid] = parse_time(stamp)
            except ValueError:
                continue

    # Empirical transition kernel: for every pair of reads up to 72 hours
    # apart, record where a game starting in a rank bucket ended up, split
    # by chart age at the earlier read.  counts[(rank, hour, age)][place]
    # holds the landings inside the top 10; totals also counts landings
    # outside it, so each bin's distribution sums to P(staying top 10).
    counts = {}
    totals = {}

    def bump(rank_b, hour_b, age_b, place):
        key = (rank_b, hour_b, age_b)
        counts.setdefault(key, [0] * places)
        counts[key][place - 1] += 1
        totals[key] = totals.get(key, 0) + 1

    read_count = len(reads)
    for i in range(read_count):
        start_time = parse_time(reads[i]["at"])
        rank_at_start = {int(row["universe_id"]): int(row["rank"])
                         for row in reads[i].get("up_and_coming") or []}
        for j in range(i + 1, read_count):
            later_time = parse_time(reads[j]["at"])
            gap = (later_time - start_time).total_seconds() / 3600.0
            if gap > 72:
                break
            hour_b = hour_bucket(max(1, int(round(gap))))
            for row in reads[j].get("up_and_coming") or []:
                uid = int(row["universe_id"])
                if uid not in rank_at_start:
                    continue
                rank_b = rank_bucket(rank_at_start[uid])
                age = (start_time - first_seen[uid]).total_seconds() / 3600.0
                age_b = 0 if age < AGE_CUT else 1
                rank2 = int(row["rank"])
                if rank2 <= places:
                    bump(rank_b, hour_b, age_b, rank2)
                else:
                    key = (rank_b, hour_b, age_b)
                    totals[key] = totals.get(key, 0) + 1

    def emp_dist(rank_b, hour_b, age_b):
        """P(place) for a bin, falling back to coarser bins when thin."""
        for key in ((rank_b, hour_b, age_b), (rank_b, hour_b, 0),
                    (rank_b, hour_b, 1), (rank_b, hour_b, None)):
            if key[2] is None:
                merged = [0] * places
                total = 0
                for other in ((rank_b, hour_b, 0), (rank_b, hour_b, 1)):
                    if other in counts:
                        for p in range(places):
                            merged[p] += counts[other][p]
                        total += totals.get(other, 0)
                if total >= 30:
                    return [value / total for value in merged]
                continue
            if totals.get(key, 0) >= 30:
                bucket = counts.get(key, [0] * places)
                total = totals[key]
                return [value / total for value in bucket]
        return None

    # Fit sigma: median |rank change| / sqrt(gap hours), scaled to a Normal
    # standard deviation, clamped to [0.25, 1.0]; 0.55 when there is no data.
    steps = []
    for uid in sorted(series):
        points = series[uid]
        for (earlier, rank_a), (later, rank_b) in zip(points, points[1:]):
            gap = (later - earlier).total_seconds() / 3600.0
            if gap > 0:
                steps.append(abs(rank_b - rank_a) / math.sqrt(gap))
    if steps:
        sigma = median(steps) / 0.6745
    else:
        sigma = 0.55
    sigma = min(SIGMA_MAX, max(SIGMA_MIN, sigma))

    # Current chart: the last read, in rank order.
    last_rows = sorted(reads[-1].get("up_and_coming") or [],
                       key=lambda row: int(row["rank"]))
    last_chart = [int(row["universe_id"]) for row in last_rows]
    current_rank = {int(row["universe_id"]): int(row["rank"]) for row in last_rows}

    def velocity(uid):
        """Recent rank drift per hour, weighted toward the newest pairs."""
        points = series[uid]
        pairs = list(zip(points, points[1:]))[-4:]
        weighted_sum = 0.0
        weight_total = 0.0
        for (earlier, rank_a), (later, rank_b) in pairs:
            gap = (later - earlier).total_seconds() / 3600.0
            if gap <= 0:
                continue
            age = (last_time - later).total_seconds() / 3600.0
            weight = 0.5 ** (age / VEL_HALFLIFE)
            weighted_sum += weight * ((rank_b - rank_a) / gap)
            weight_total += weight
        if weight_total == 0:
            return 0.0
        value = weighted_sum / weight_total
        return min(VEL_CLAMP, max(-VEL_CLAMP, value))

    # Average per-place probabilities over the forecast hours.
    averages = {}
    for uid in sorted(current_rank):
        drift = velocity(uid)
        born = created.get(uid)
        expiry = born + timedelta(days=28) if born is not None else None
        rank_b = rank_bucket(current_rank[uid])
        age_now = (last_time - first_seen[uid]).total_seconds() / 3600.0
        age_b = 0 if age_now < AGE_CUT else 1
        sums = [0.0] * places
        for hour in range(1, slots + 1):
            # Past expiry every later hour is expired too, so stop here.
            if expiry is not None and last_time + timedelta(hours=hour) > expiry:
                break
            displacement = drift * VEL_TAU * (1.0 - math.exp(-hour / VEL_TAU))
            mean = current_rank[uid] + displacement
            spread = min(SPREAD_CAP, sigma * math.sqrt(hour))
            hour_b = hour_bucket(hour)
            emp = emp_dist(rank_b, hour_b, age_b)
            normal_top = norm_cdf((1.5 - mean) / spread)
            sums[0] += ((1.0 - ALPHA) * normal_top
                        + (ALPHA * emp[0] if emp else 0.0))
            for place in range(2, places + 1):
                normal_prob = (norm_cdf((place + 0.5 - mean) / spread)
                               - norm_cdf((place - 0.5 - mean) / spread))
                sums[place - 1] += ((1.0 - ALPHA) * normal_prob
                                    + (ALPHA * emp[place - 1]
                                       if emp else 0.0))
        averages[uid] = [total / slots for total in sums]
        if TEMP != 1.0:
            averages[uid] = [value ** TEMP for value in averages[uid]]

    # Per place: keep the best candidates above 0.003, at most 30, and
    # rescale them to sum to exactly 0.75, rounded to 6 decimals.
    rows = []
    for place in range(1, places + 1):
        candidates = [(averages[uid][place - 1], uid)
                      for uid in sorted(averages)
                      if averages[uid][place - 1] > FLOOR]
        candidates.sort(key=lambda item: (-item[0], item[1]))
        candidates = candidates[:CAP]
        total = sum(prob for prob, _ in candidates)
        if total <= 0:
            continue
        scaled = [(uid, prob / total * NAMED) for prob, uid in candidates]
        rounded = [(uid, round(prob, 6)) for uid, prob in scaled]
        drift = round(NAMED - sum(prob for _, prob in rounded), 6)
        if drift and rounded:
            widest = max(range(len(rounded)),
                         key=lambda index: rounded[index][1])
            uid, prob = rounded[widest]
            rounded[widest] = (uid, round(prob + drift, 6))
        for uid, prob in rounded:
            rows.append({"universe_id": uid, "place": place, "p": prob})
    rows.sort(key=lambda row: (row["place"], -row["p"], row["universe_id"]))

    # top10: greedy walk over rows by descending p, first sighting of each
    # game wins, then order the ten by the place they were taken at.
    ordered = sorted(rows, key=lambda row: (-row["p"], row["place"],
                                            row["universe_id"]))
    picked = []
    seen = set()
    for row in ordered:
        uid = row["universe_id"]
        if uid in seen:
            continue
        seen.add(uid)
        picked.append((uid, row["place"]))
        if len(picked) == places:
            break
    picked.sort(key=lambda item: item[1])
    top10 = [uid for uid, _ in picked]
    if len(top10) < places:
        for uid in last_chart:
            if uid not in seen:
                top10.append(uid)
                seen.add(uid)
                if len(top10) == places:
                    break

    forecast = {"probabilities": rows, "top10": top10}
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, output_name), "w",
              encoding="utf-8") as handle:
        handle.write(json.dumps(forecast) + "\n")


if __name__ == "__main__":
    main()
