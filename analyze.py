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
  python3 analyze.py --cost          # API-price cost split + model-swap scenarios
"""
import argparse, collections, glob, json, os, re, statistics, sys, time

ROOT = os.path.expanduser("~/.claude")

# $ per million tokens: input, cache write 5m, cache write 1h, cache read, output.
# Anthropic API list prices as of 2026-09; update when they change.
PRICES = {
    "claude-fable-5-1": (10, 12.5, 20, 0.25, 50),
    "claude-fable-5": (10, 12.5, 20, 1, 50),
    "claude-opus-5-5": (4, 5, 8, 0.2, 20),
    "claude-opus-5": (5, 6.25, 10, 0.5, 25),
    "claude-sonnet-5": (2, 2.5, 4, 0.2, 10),
    "claude-haiku-4-5": (1, 1.25, 2, 0.1, 5),
}


def price(model):
    # longest prefix wins, so claude-opus-5-5 is not priced as claude-opus-5
    for k in sorted(PRICES, key=len, reverse=True):
        if model.startswith(k):
            return PRICES[k]
    return None


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


def usage_rows(f):
    """Per-model [input, cw5m, cw1h, cache read, output] for one log file."""
    seen, rows = set(), collections.defaultdict(lambda: [0] * 5)
    for line in open(f, errors="ignore"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = d.get("message")
        if d.get("type") != "assistant" or not isinstance(m, dict):
            continue
        u, mid, model = m.get("usage"), m.get("id"), m.get("model", "?")
        if not u or mid in seen or model == "<synthetic>":
            continue
        seen.add(mid)
        cc = u.get("cache_creation") or {}
        c5, c1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
        if c5 is None and c1 is None:
            c5, c1 = u.get("cache_creation_input_tokens", 0), 0
        v = [u.get("input_tokens", 0), c5 or 0, c1 or 0,
             u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0)]
        for i in range(5):
            rows[model.split("[")[0]][i] += v[i]
    return rows


def cost_report(days):
    # bucket: (main|sub, model of the parent main session, model) -> token row
    b = collections.defaultdict(lambda: [0] * 5)
    files = list(iter_files(days))
    parent = {}
    for f in files:
        if "/subagents/" in f:
            continue
        rows = usage_rows(f)
        parent[f] = max(rows, key=lambda k: sum(rows[k])) if rows else "?"
        for mo, v in rows.items():
            b[("main", parent[f], mo)] = [x + y for x, y in zip(b[("main", parent[f], mo)], v)]
    for f in files:
        if "/subagents/" not in f:
            continue
        p = parent.get(f.split("/subagents/")[0] + ".jsonl", "?")
        for mo, v in usage_rows(f).items():
            b[("sub", p, mo)] = [x + y for x, y in zip(b[("sub", p, mo)], v)]

    def dollars(v, mo):
        pr = price(mo)
        return sum(x * y for x, y in zip(v, pr)) / 1e6 if pr else 0

    unpriced = {mo for (_, _, mo) in b if not price(mo)}
    total = sum(dollars(v, mo) for (_, _, mo), v in b.items())
    print(f"# API-price cost — last {days} days (list prices; subscription weighting is not published)\n")
    print(f"total ≈ ${total:,.0f}" + (f"   (unpriced models skipped: {', '.join(sorted(unpriced))})" if unpriced else ""))

    print("\n## By model")
    bm = collections.defaultdict(lambda: [0.0, 0])
    for (_, _, mo), v in b.items():
        bm[mo][0] += dollars(v, mo)
        bm[mo][1] += sum(v)
    for mo, (c, t) in sorted(bm.items(), key=lambda x: -x[1][0]):
        print(f"  {mo[:28]:28} ${c:9,.0f} {c / max(total, 1e-9):6.1%}  {fmt(t):>7} tokens  ${c / max(t, 1) * 1e6:.2f}/M")

    print("\n## Main session vs its subagents, by main-session model")
    grp = collections.defaultdict(lambda: [0.0, 0])
    for (kind, p, mo), v in b.items():
        grp[(p, kind)][0] += dollars(v, mo)
        grp[(p, kind)][1] += sum(v)
    for p in sorted({p for p, _ in grp}, key=lambda p: -sum(grp[(p, k)][0] for k in ("main", "sub"))):
        (mc, mt), (sc, st) = grp.get((p, "main"), [0, 0]), grp.get((p, "sub"), [0, 0])
        ratio = f"1:{sc / mc:.1f} in $, 1:{st / max(mt, 1):.1f} in tokens" if mc else ""
        print(f"  {p[:24]:24} main ${mc:8,.0f}  subagents ${sc:8,.0f}  {ratio}")

    def swap(rule):
        return sum(dollars(v, rule(kind, mo)) for (kind, _, mo), v in b.items())

    old_opus = lambda mo: mo.startswith("claude-opus-5") and not mo.startswith("claude-opus-5-5")
    fable = lambda mo: mo.startswith("claude-fable")
    print("\n## Same tokens, different models (assumes token counts stay the same)")
    for name, rule in [
        ("Fable main sessions -> Opus 5.5", lambda k, mo: "claude-opus-5-5" if k == "main" and fable(mo) else mo),
        ("Opus 5 (all) -> Opus 5.5", lambda k, mo: "claude-opus-5-5" if old_opus(mo) else mo),
        ("both", lambda k, mo: "claude-opus-5-5" if (k == "main" and fable(mo)) or old_opus(mo) else mo),
        ("Opus subagents -> Sonnet 5", lambda k, mo: "claude-sonnet-5" if k == "sub" and mo.startswith("claude-opus") else mo),
    ]:
        c = swap(rule)
        print(f"  {name:34} ${c:9,.0f}  ({c / max(total, 1e-9) - 1:+.0%})")


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
    ap.add_argument("--cost", action="store_true", help="API-price cost split and model-swap scenarios")
    a = ap.parse_args()
    if a.cost:
        return cost_report(a.days)
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
