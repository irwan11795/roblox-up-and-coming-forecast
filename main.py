#!/usr/bin/env python3
"""Deterministic rank-diffusion forecast for the Roblox Up-and-Coming top 10.

Reads the chart history from $MAKEFUN_INPUT_DIR (default /input) and writes
one static forecast to $MAKEFUN_OUTPUT_DIR (default /output).

Model: each game on the last chart keeps its last rank, drifts with an
exponentially weighted recent velocity that saturates over a 6-hour
timescale, and spreads with a diffusion width fitted from the history
itself.  Per-place probabilities are averaged over the 72 forecast hours,
then the best candidates at each place are rescaled to sum to 0.70.
Games age out of the chart 28 days after creation when that date is known.
"""

import json
import math
import os
from datetime import datetime, timedelta


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

    # Per-game series of (timestamp, rank), in time order, plus creation times.
    series = {}
    created = {}
    for rec in reads:
        when = parse_time(rec["at"])
        for row in rec.get("up_and_coming") or []:
            uid = int(row["universe_id"])
            series.setdefault(uid, []).append((when, int(row["rank"])))
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
    sigma = min(1.0, max(0.25, sigma))

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
            weight = 0.5 ** (age / 48.0)
            weighted_sum += weight * ((rank_b - rank_a) / gap)
            weight_total += weight
        if weight_total == 0:
            return 0.0
        value = weighted_sum / weight_total
        return min(1.5, max(-1.5, value))

    # Average per-place probabilities over the forecast hours.
    averages = {}
    for uid in sorted(current_rank):
        drift = velocity(uid)
        born = created.get(uid)
        expiry = born + timedelta(days=28) if born is not None else None
        totals = [0.0] * places
        for hour in range(1, slots + 1):
            # Past expiry every later hour is expired too, so stop here.
            if expiry is not None and last_time + timedelta(hours=hour) > expiry:
                break
            displacement = drift * 6.0 * (1.0 - math.exp(-hour / 6.0))
            mean = current_rank[uid] + displacement
            spread = min(14.0, sigma * math.sqrt(hour))
            totals[0] += norm_cdf((1.5 - mean) / spread)
            for place in range(2, places + 1):
                upper = norm_cdf((place + 0.5 - mean) / spread)
                lower = norm_cdf((place - 0.5 - mean) / spread)
                totals[place - 1] += upper - lower
        averages[uid] = [total / slots for total in totals]

    # Per place: keep the best candidates above 0.003, at most 30, and
    # rescale them to sum to exactly 0.70, rounded to 6 decimals.
    rows = []
    for place in range(1, places + 1):
        candidates = [(averages[uid][place - 1], uid)
                      for uid in sorted(averages)
                      if averages[uid][place - 1] > 0.003]
        candidates.sort(key=lambda item: (-item[0], item[1]))
        candidates = candidates[:30]
        total = sum(prob for prob, _ in candidates)
        if total <= 0:
            continue
        scaled = [(uid, prob / total * 0.70) for prob, uid in candidates]
        rounded = [(uid, round(prob, 6)) for uid, prob in scaled]
        drift = round(0.70 - sum(prob for _, prob in rounded), 6)
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
