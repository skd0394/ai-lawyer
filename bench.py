#!/usr/bin/env python3
"""Benchmark against DocDraft's reference queries.

    python3 bench.py            # 3 warm runs per query + 1 cold
    python3 bench.py --runs 5
    python3 bench.py --only demand
    python3 bench.py --report   # re-print from saved results, no API calls

Writes bench_results.json and prints a markdown table for BENCHMARKS.md.

METHODOLOGY (state this in the writeup, it matters):
  - a dedicated empty user volume: file clutter was measured to change the
    agent's behaviour, not just its token count
  - a fresh session per run, so history never accumulates across runs
  - one discarded warm-up run before measuring
  - cold start measured separately by terminating the sandbox first
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time

BASE = os.environ.get(
    "BASE",
    "https://docdraft-developers-kartik--ailaw-kartik-fastapi-app.modal.run")
USER = os.environ.get("BENCH_USER", "bench")
RESULTS = "bench_results.json"

# Verbatim from the requirements document. Do not paraphrase — the whole
# point is that these are the same inputs.
QUERIES = {
    "employment": {
        "prompt": ("Draft a new employment agreement for a fin-tech startup "
                   "in New York City. The jurisdiction is New York state law."),
        "ref": {"input": 135600, "output": 13830, "total": 149400,
                "cost": 0.358, "latency_avg": 257, "latency_med": 236},
    },
    "demand": {
        "prompt": ("Draft a demand letter for unpaid invoices totaling "
                   "$18,500 owed to a marketing agency in Austin, Texas by a "
                   "former client in Austin, Texas. The jurisdiction is "
                   "Texas Law."),
        "ref": {"input": 144150, "output": 6740, "total": 150900,
                "cost": 0.205, "latency_avg": 143, "latency_med": 133},
    },
    "operating": {
        "prompt": ("Draft an LLC operating agreement for a two-member "
                   "consulting business in Chicago, Illinois with 60/40 "
                   "ownership. The jurisdiction is Illinois state law."),
        "ref": {"input": 135890, "output": 15050, "total": 150900,
                "cost": 0.361, "latency_avg": 270, "latency_med": 275},
    },
}


def curl(args, timeout=600):
    return subprocess.run(["curl", "-s"] + args, capture_output=True,
                          text=True, timeout=timeout).stdout


def run_turn(prompt, session_id, max_iterations=16):
    """One turn. Returns the parsed metrics, or None on failure."""
    body = json.dumps({"prompt": prompt, "session_id": session_id,
                       "user_id": USER, "max_iterations": max_iterations})
    t0 = time.perf_counter()
    out = curl(["-X", "POST", f"{BASE}/api/chat",
                "-H", "Content-Type: application/json", "-d", body])
    wall_ms = int((time.perf_counter() - t0) * 1000)

    usages, tools, citations = [], [], []
    doc = None
    end = None
    text = ""

    for line in out.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            ev = json.loads(line[6:])
        except Exception:
            continue
        t = ev.get("type")
        if t == "usage":
            usages.append(ev)
        elif t == "tool_end":
            tools.append(ev["name"])
        elif t == "citation":
            citations.append(ev["confidence"])
        elif t == "text_delta":
            text += ev["text"]
        elif t == "structured_block" and ev.get("kind") == "document":
            doc = ev["payload"]
        elif t == "turn_end":
            end = ev

    if end is None:
        return {"ok": False, "error": out[:400] or "no turn_end", "wall_ms": wall_ms}

    return {
        "ok": True,
        "turn_id": None,
        "stop_reason": end["stop_reason"],
        "input": end["billed_input_tokens"],
        "output": end["billed_output_tokens"],
        "total": end["billed_input_tokens"] + end["billed_output_tokens"],
        "cost": end["cost_usd"],
        "ttft_ms": end.get("ttft_ms"),
        "total_ms": end["total_ms"],
        "wall_ms": wall_ms,
        # Decomposition — the interesting part for the writeup.
        "uncached_input": sum(u["input_tokens"] for u in usages),
        "cache_read": sum(u["cache_read_input_tokens"] for u in usages),
        "cache_write": sum(u["cache_creation_input_tokens"] for u in usages),
        "model_calls": len(usages),
        "by_model": {m: sum(1 for u in usages if u["model"] == m)
                     for m in {u["model"] for u in usages}},
        "tool_calls": len(tools),
        "tools": tools,
        "searches": tools.count("web_search"),
        "fetches": tools.count("web_fetch"),
        "citations": {c: citations.count(c)
                      for c in ("verified", "partial", "unverified")
                      if citations.count(c)},
        "document": (doc or {}).get("filename"),
        "words": (doc or {}).get("word_count"),
        "placeholders": len((doc or {}).get("placeholders") or []),
        "answer_chars": len(text),
    }


def clear_outputs():
    """Wipe generated files before each run.

    Every drafting run writes a .docx to outputs/, which then appears in
    the NEXT run's file manifest. By run 5 the agent is looking at a dozen
    operating agreements it wrote itself — and a cluttered manifest was
    measured to change its behaviour, not just its token count. Without
    this the benchmark poisons itself.
    """
    try:
        d = json.loads(curl([f"{BASE}/api/files?user_id={USER}"]))["files"]
    except Exception:
        return 0
    n = 0
    for area, items in d.items():
        for f in items:
            curl(["-X", "DELETE",
                  f"{BASE}/api/files/{area}/{f['name']}?user_id={USER}"])
            n += 1
    return n


def reset_sandbox():
    curl(["-X", "DELETE", f"{BASE}/api/debug/sandbox?user_id={USER}"])
    curl(["-X", "POST", f"{BASE}/api/turn/force-release?user_id={USER}"])


def stat(rows, key):
    vals = [r[key] for r in rows if r.get("ok") and r.get(key) is not None]
    if not vals:
        return {"avg": None, "med": None, "min": None, "max": None}
    return {"avg": round(statistics.mean(vals), 4),
            "med": round(statistics.median(vals), 4),
            "min": min(vals), "max": max(vals)}


def measure(name, runs):
    q = QUERIES[name]
    print(f"\n{'='*66}\n{name.upper()}\n{'='*66}")

    print("  cold start (sandbox terminated first)...", flush=True)
    clear_outputs()
    reset_sandbox()
    cold = run_turn(q["prompt"], f"bench-{name}-cold-{int(time.time())}")
    if cold.get("ok"):
        print(f"    {cold['total_ms']/1000:.1f}s  in {cold['input']:,}  "
              f"out {cold['output']:,}  ${cold['cost']}")
    else:
        print(f"    FAILED: {cold.get('error')}")

    print("  warm-up (discarded)...", flush=True)
    clear_outputs()
    run_turn(q["prompt"], f"bench-{name}-warmup-{int(time.time())}")

    rows = []
    for i in range(runs):
        print(f"  run {i+1}/{runs}...", end=" ", flush=True)
        clear_outputs()          # identical starting state every run
        r = run_turn(q["prompt"], f"bench-{name}-{i}-{int(time.time())}")
        rows.append(r)
        if r.get("ok"):
            doc = (f"{r['words']:,}w" if r.get("words")
                   else "\033[33mNO DOCUMENT\033[0m")
            print(f"{r['total_ms']/1000:.1f}s  in {r['input']:,}  "
                  f"out {r['output']:,}  ${r['cost']}  "
                  f"[{r['searches']}s/{r['fetches']}f]  {doc}  "
                  f"{r['stop_reason']}")
        else:
            print(f"FAILED: {r.get('error')}")

    # The benchmark is document generation. A run that produced no
    # document is a failure to measure, not a cheap success — including it
    # would flatter the median. Excluded here and reported separately.
    valid = [r for r in rows if r.get("ok") and r.get("words")]
    return {"cold": cold, "runs": rows, "valid": len(valid),
            "attempted": len([r for r in rows if r.get("ok")]),
            "stats": {k: stat(valid, k) for k in
                      ("input", "output", "total", "cost", "total_ms",
                       "ttft_ms", "cache_read", "cache_write",
                       "uncached_input", "tool_calls", "searches", "fetches",
                       "model_calls", "words", "placeholders")}}


def report(data):
    print("\n\n" + "="*66)
    print("PASTE INTO BENCHMARKS.md")
    print("="*66 + "\n")

    print("## Reference queries — measured vs DocDraft\n")
    print("_Medians over runs that produced a document. "
          "DocDraft's figures are averages over 108 runs; n here is small, "
          "so the range table below matters as much as the median._\n")
    print("| Query | Metric | DocDraft | This system | Δ |")
    print("|---|---|---:|---:|---:|")
    for name, d in data["queries"].items():
        ref, st = QUERIES[name]["ref"], d["stats"]
        def row(lbl, refv, got, fmt="{:,.0f}", better_low=True):
            if got is None:
                print(f"| {name} | {lbl} | {fmt.format(refv)} | — | — |")
                return
            ratio = (refv / got) if got else 0
            mark = (f"**{ratio:.1f}× lower**" if better_low and ratio > 1
                    else f"{1/ratio:.1f}× higher" if better_low and ratio
                    else "—")
            print(f"| {name} | {lbl} | {fmt.format(refv)} | "
                  f"{fmt.format(got)} | {mark} |")
        # DocDraft reported averages over 108 runs. At n=3 with this much
        # spread the median is the more honest central estimate, and the
        # range below tells the real story.
        row("input tokens", ref["input"], st["input"]["med"])
        row("output tokens", ref["output"], st["output"]["med"])
        row("total tokens", ref["total"], st["total"]["med"])
        row("cost (USD)", ref["cost"], st["cost"]["med"], "${:.3f}")
        row("latency (s)", ref["latency_med"],
            (st["total_ms"]["med"] or 0) / 1000, "{:.0f}")
        print(f"| | | | | |")

    print("\n## Detail\n")
    print("| Query | uncached in | cache read | cache write | model calls "
          "| searches | fetches | words | placeholders | cold start |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for name, d in data["queries"].items():
        s = d["stats"]
        cold = d["cold"].get("total_ms")
        print(f"| {name} | {s['uncached_input']['avg'] or 0:,.0f} "
              f"| {s['cache_read']['avg'] or 0:,.0f} "
              f"| {s['cache_write']['avg'] or 0:,.0f} "
              f"| {s['model_calls']['avg'] or 0:.1f} "
              f"| {s['searches']['avg'] or 0:.1f} "
              f"| {s['fetches']['avg'] or 0:.1f} "
              f"| {s['words']['avg'] or 0:,.0f} "
              f"| {s['placeholders']['avg'] or 0:.1f} "
              f"| {(cold/1000 if cold else 0):.0f}s |")

    print("\n## Variance (warm runs)\n")
    print("| Query | n | input min–max | latency min–max | documents | stop reasons |")
    print("|---|---:|---|---|---:|---|")
    for name, d in data["queries"].items():
        ok = [r for r in d["runs"] if r.get("ok")]
        s = d["stats"]
        stops = {}
        for r in ok:
            stops[r["stop_reason"]] = stops.get(r["stop_reason"], 0) + 1
        docs = d.get("valid", sum(1 for r in ok if r.get("words")))
        print(f"| {name} | {len(ok)} "
              f"| {s['input']['min'] or 0:,}–{s['input']['max'] or 0:,} "
              f"| {(s['total_ms']['min'] or 0)/1000:.0f}–"
              f"{(s['total_ms']['max'] or 0)/1000:.0f}s "
              f"| {docs}/{len(ok)} "
              f"| {', '.join(f'{k}×{v}' for k,v in stops.items())} |")

    if not data.get("pricing_verified"):
        print("\n> ⚠️ Cost figures use UNVERIFIED placeholder rates. Set "
              "`PRICING_LAST_VERIFIED` in obs/pricing.py after checking "
              "the pricing page, then re-run.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--only", default=None)
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    if a.report:
        report(json.load(open(RESULTS)))
        return

    health = curl([f"{BASE}/api/health"])
    try:
        pricing_verified = "unverified" not in health.lower()
    except Exception:
        pricing_verified = False

    files = curl([f"{BASE}/api/files?user_id={USER}"])
    try:
        n = sum(len(v) for v in json.loads(files)["files"].values())
        if n:
            print(f"⚠️  the '{USER}' volume has {n} file(s). A cluttered "
                  f"manifest changes agent behaviour — clear it or use a "
                  f"different BENCH_USER.")
    except Exception:
        pass

    names = [a.only] if a.only else list(QUERIES)
    data = {"at": time.time(), "base": BASE, "user": USER,
            "runs_per_query": a.runs, "pricing_verified": pricing_verified,
            "queries": {}}

    t0 = time.time()
    for nm in names:
        data["queries"][nm] = measure(nm, a.runs)
        json.dump(data, open(RESULTS, "w"), indent=2)

    print(f"\ntotal wall time: {(time.time()-t0)/60:.1f} min")
    print(f"raw results: {RESULTS}")
    report(data)


if __name__ == "__main__":  
    main()