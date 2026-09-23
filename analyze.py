#!/usr/bin/env python3
"""Audit where your Claude Code tokens go, using only local session logs.

Reads ~/.claude/projects/**/*.jsonl (nothing is uploaded anywhere) and reports:
  - tokens by model (fresh input / cache write / cache read / output)
  - main session vs subagent share
  - fixed context at session start (system prompt + tools + skills + CLAUDE.md)
  - how much of your cache reads is old tool output being re-read
  - mid-session model switches and the cache writes they caused
  - which skills were actually invoked, and a skillOverrides suggestion

Usage:
  python3 analyze.py                 # last 14 days
  python3 analyze.py --days 30
  python3 analyze.py --suggest-overrides > overrides.json
"""
import argparse, collections, glob, json, os, re, statistics, sys, time

ROOT = os.path.expanduser("~/.claude")


def iter_files(days):
    cut = time.time() - days * 86400
    for f in glob.glob(f"{ROOT}/projects/*/**/*.jsonl", recursive=True):
        if os.path.getmtime(f) >= cut:
            yield f


def result_tokens(content):
    """Rough token count of a tool_result (text ~3.5 chars/token, image ~1.6k)."""
    if isinstance(content, str):
        return len(content) / 3.5
    n = 0
    for b in content or []:
        if isinstance(b, dict):
            if b.get("type") == "text":
                n += len(b.get("text", "")) / 3.5
            elif b.get("type") == "image":
                n += 1600
    return n


def analyze(days):
    by_model = collections.defaultdict(lambda: [0, 0, 0, 0, 0])  # in, cw, cr, out, calls
    side = {"main": 0, "subagent": 0}
    sessions = {"main": [], "subagent": []}
    tools, skills = collections.Counter(), collections.Counter()
    reread = collections.Counter()
    switches, switch_cw = 0, 0
    seen_tool = set()

    for f in iter_files(days):
        kind = "subagent" if "/subagents/" in f else "main"
        seen, ev, idname = set(), [], {}
        first = peak = calls = 0
        prev_model = None
        for line in open(f, errors="ignore"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
                ev.append(("compact",))
            m = d.get("message")
            if not isinstance(m, dict):
                continue
            content = m.get("content")
            if d.get("type") == "assistant":
                for b in content or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") not in seen_tool:
                        seen_tool.add(b.get("id"))
                        idname[b["id"]] = b["name"]
                        tools[b["name"]] += 1
                        if b["name"] == "Skill":
                            skills[(b.get("input") or {}).get("skill")] += 1
                u, mid, model = m.get("usage"), m.get("id"), m.get("model", "?")
                if not u or mid in seen or model == "<synthetic>":
                    continue
                seen.add(mid)
                v = [u.get("input_tokens", 0), u.get("cache_creation_input_tokens", 0),
                     u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0), 1]
                for i in range(5):
                    by_model[model][i] += v[i]
                ctx = v[0] + v[1] + v[2]
                side[kind] += ctx
                calls += 1
                first = first or ctx
                peak = max(peak, ctx)
                if kind == "main" and prev_model and model != prev_model:
                    switches += 1
                    switch_cw += v[1]
                prev_model = model
                ev.append(("call",))
            elif d.get("type") == "user" and isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        ev.append(("result", idname.get(b.get("tool_use_id"), "?"),
                                   result_tokens(b.get("content"))))
        if calls:
            sessions[kind].append((calls, first, peak))
        # each tool result is re-read by every later call until the next compaction
        for i, e in enumerate(ev):
            if e[0] != "result":
                continue
            k = 0
            for x in ev[i + 1:]:
                if x[0] == "compact":
                    break
                k += x[0] == "call"
            reread[e[1]] += e[2] * k

    return dict(by_model=by_model, side=side, sessions=sessions, tools=tools, skills=skills,
                reread=reread, switches=switches, switch_cw=switch_cw)


def installed_skills():
    names = set()
    for f in glob.glob(f"{ROOT}/skills/**/SKILL.md", recursive=True):
        txt = open(f, errors="ignore").read(3000)
        m = re.search(r"^name:\s*[\"']?([^\"'\n]+)", txt, re.M)
        names.add(m.group(1).strip() if m else os.path.basename(os.path.dirname(f)))
    return names


def fmt(n):
    return f"{n / 1e9:.2f}B" if n >= 1e9 else f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


def report(r, days):
    bm = r["by_model"]
    cr = sum(v[2] for v in bm.values())
    cw = sum(v[1] for v in bm.values())
    out = sum(v[3] for v in bm.values())
    print(f"# Claude Code token audit — last {days} days\n")
    print("## By model")
    print(f"{'model':32} {'cache read':>11} {'cache write':>12} {'output':>9} {'calls':>7}")
    for k, v in sorted(bm.items(), key=lambda x: -x[1][2]):
        print(f"{k[:32]:32} {fmt(v[2]):>11} {fmt(v[1]):>12} {fmt(v[3]):>9} {v[4]:>7}")
    print(f"\ncache write / cache read = {cw / max(cr, 1):.1%}  (low = cache is working)")
    # relative cost weights vs base input price: read 0.1x, write 1.25x (5m) .. 2x (1h), output 5x
    lo, hi = cr * 0.1 + cw * 1.25 + out * 5, cr * 0.1 + cw * 2 + out * 5
    print(f"output share of weighted cost ≈ {out * 5 / hi:.0%}–{out * 5 / lo:.0%}")

    s = r["side"]
    tot = s["main"] + s["subagent"]
    print("\n## Main session vs subagents (input-side tokens)")
    print(f"main {fmt(s['main'])}  subagent {fmt(s['subagent'])}  → subagents = {s['subagent'] / max(tot, 1):.0%}")
    for kind, L in r["sessions"].items():
        if L:
            print(f"{kind:9} n={len(L):5}  median calls={statistics.median(x[0] for x in L):.0f}"
                  f"  median first-turn context={fmt(statistics.median(x[1] for x in L))}"
                  f"  median peak={fmt(statistics.median(x[2] for x in L))}")

    rr = sum(r["reread"].values())
    print("\n## Old tool output re-read on later calls (estimate)")
    print(f"≈{fmt(rr)} = {rr / max(cr, 1):.0%} of cache reads")
    for k, v in r["reread"].most_common(5):
        print(f"  {k:12} {fmt(v)}")

    print(f"\n## Mid-session model switches: {r['switches']}  (extra cache write ≈{fmt(r['switch_cw'])})")
    print("\n## Top tools")
    print("  " + ", ".join(f"{k} {v}" for k, v in r["tools"].most_common(10)))
    inst = installed_skills()
    used = {k for k in r["skills"] if k}
    print(f"\n## Skills: {len(inst)} installed in ~/.claude/skills, {len(used)} invoked in {days} days")
    print("  " + ", ".join(f"{k} {v}" for k, v in r["skills"].most_common(20)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--suggest-overrides", action="store_true",
                    help="print a skillOverrides block: used skills 'on', the rest 'user-invocable-only'")
    a = ap.parse_args()
    r = analyze(a.days)
    if a.suggest_overrides:
        used = set(r["skills"])
        ov = {n: "on" if n in used else "user-invocable-only" for n in sorted(installed_skills())}
        json.dump({"skillOverrides": ov}, sys.stdout, indent=2, ensure_ascii=False)
        print()
    else:
        report(r, a.days)


if __name__ == "__main__":
    main()
