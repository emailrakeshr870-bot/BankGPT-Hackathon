"""
Scoring rules (identical for the public and the internal eval).

Per question (0..1):
  correctness  - share of `must_include` facts present in the answer
                 (numbers compared by value: "3.5%" == "3.50 %", "$2.5" == "$2.50")
  trap         - any `must_not_include` value present (stale / wrong / other-tier)
                 -> correctness is zeroed. Repeating an old value IS the failure we test.
  refusal      - category `should_refuse`: full credit only if the system declines
                 (confident=False or a clear "I don't know / contact us") and states no number.
  sources      - +credit if any cited URL matches `expected_sources` (path-level match).

  item score = 0.85 * correctness + 0.15 * sources        (sources term dropped when
                                                            the item lists no sources)

Final score = four pillars (weights renormalised over the pillars present):
  accuracy     55%  weighted mean of item scores (weight field, default 1)
  conflicts    15%  0.7 * F1 vs the gold conflict list + 0.3 * picked-the-right-winner
  consistency  15%  reworded questions (same `group`) agree + a repeat pass gives the same values
  speed        15%  p95 answer latency: <=2s -> 1.0, linearly down to 0 at >=20s
  (consistency and speed are multiplied by the accuracy score)
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from urllib.parse import urlparse

NUM_RE = re.compile(r"(\$)?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(%|percent)?", re.I)
REFUSAL_RE = re.compile(
    r"\b(i (?:do not|don't|can't|cannot) (?:know|find|confirm)|not (?:sure|able to confirm)|"
    r"no (?:longer )?(?:available|offered)|isn't listed|is not listed|please (?:contact|call|visit)|"
    r"no information)\b",
    re.I,
)


def _numbers(text: str) -> set[tuple[str, float]]:
    out = set()
    for m in NUM_RE.finditer(text or ""):
        unit = "$" if m.group(1) else ("%" if m.group(4) else "")
        val = float(m.group(2).replace(",", "") + ("." + m.group(3) if m.group(3) else ""))
        out.add((unit, round(val, 4)))
    return out


def _norm(s: str) -> str:
    s = re.sub(r"\b([ap])\.\s?m\.?", r"\1m", (s or "").lower())   # "a.m." / "a.m" -> "am"
    s = s.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip()


def contains(answer: str, token: str) -> bool:
    """Value-aware containment: numeric tokens match by value+unit, text by substring."""
    tnums = _numbers(token)
    # Compare by value only for pure numbers/rates ("3%", "$2.50", "5.39% APR"); anything with
    # other words ("24 branch", "8 a.m.") is matched as text so "24-hour ATM" can't trip it.
    leftover = re.sub(r"[\s$\d.,%]|\b(apr|apy|percent)\b", "", token.lower())
    if tnums and not leftover and len(token) <= 20:
        anums = _numbers(answer)
        return all(n in anums or (n[0] == "" and any(a[1] == n[1] for a in anums)) for n in tnums)
    return _norm(token) in _norm(answer)


def _url_key(u: str) -> str:
    p = urlparse(u if "//" in u else "https://" + u)
    return (p.netloc.lower().removeprefix("www.") + p.path.rstrip("/").lower())


def _llm_judge(item: dict, answer_text: str) -> float | None:
    """Optional semantic check for free-text answers. Returns None if unavailable."""
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        return None
    try:
        import anthropic
        from pydantic import BaseModel

        class Verdict(BaseModel):
            score: float
            reason: str

        client = anthropic.Anthropic()
        resp = client.messages.parse(
            model="claude-opus-5-5",
            max_tokens=2000,
            output_config={"effort": "low"},
            messages=[{
                "role": "user",
                "content": (
                    "Grade a credit-union assistant's answer against the reference.\n"
                    f"Question: {item['question']}\nReference answer: {item['expected_answer']}\n"
                    f"Values that must NOT appear (stale/wrong): {item.get('must_not_include', [])}\n"
                    f"Candidate answer: {answer_text}\n\n"
                    "score = 1 if the candidate states the reference facts correctly and nothing "
                    "contradicting them, 0.5 if partially correct, 0 if wrong, stale, or evasive."
                ),
            }],
            output_format=Verdict,
        )
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            return None
        return max(0.0, min(1.0, resp.parsed_output.score))
    except Exception:
        return None


def score_item(item: dict, ans, judge: bool = False) -> dict:
    text = ans.text or ""
    must = item.get("must_include", [])
    must_not = item.get("must_not_include", [])
    cat = item.get("category", "general")

    hit = [t for t in must if contains(text, t)]
    trap = [t for t in must_not if contains(text, t)]
    refused = (not ans.confident) or bool(REFUSAL_RE.search(text))

    if cat == "should_refuse":
        correctness = 1.0 if refused and not trap and not _numbers(text) - _numbers(item["question"]) else 0.0
    else:
        correctness = (len(hit) / len(must)) if must else 0.0
        if judge and item.get("expected_answer"):
            j = _llm_judge(item, text)
            if j is not None:
                correctness = 0.5 * correctness + 0.5 * j if must else j
        if trap:
            correctness = 0.0

    exp_src = {_url_key(u) for u in item.get("expected_sources", [])}
    got_src = {_url_key(s.url) for s in (ans.sources or []) if getattr(s, "url", None)}
    if exp_src:
        src = 1.0 if exp_src & got_src else 0.0
        score = 0.85 * correctness + 0.15 * src
    else:
        src, score = None, correctness

    return {
        "id": item["id"], "category": cat, "group": item.get("group"), "weight": item.get("weight", 1.0),
        "score": round(score, 4), "correctness": round(correctness, 4), "source_hit": src,
        "matched": hit, "missed": [t for t in must if t not in hit], "trap_hits": trap,
    }


def score_conflicts(gold: list[dict], submitted) -> dict:
    """Gold entry: {id, topic_keywords:[...], values:[...]} — matched when the submitted
    conflict names >=2 of the gold values, or >=1 value plus a topic keyword.
    Resolution credit when gold has `correct_value` and the submission resolved to it."""
    subs = []
    for c in submitted or []:
        vals = " | ".join(getattr(c, "values", []) or [])
        subs.append((_norm(getattr(c, "topic", "") or ""), vals, getattr(c, "resolution", None) or ""))
    matched, resolved_ok, used = 0, 0, set()
    for g in gold:
        for i, (topic, vals, res) in enumerate(subs):
            if i in used:
                continue
            vhits = sum(contains(vals, v) for v in g["values"])
            khit = any(_norm(k) in topic for k in g.get("topic_keywords", []))
            if vhits >= 2 or (vhits >= 1 and khit):
                matched += 1
                used.add(i)
                if g.get("correct_value") and res and contains(res, g["correct_value"]):
                    resolved_ok += 1
                break
    recall = matched / len(gold) if gold else 0.0
    precision = matched / len(subs) if subs else 0.0
    f = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    n_res = sum(1 for g in gold if g.get("correct_value"))
    return {
        "gold": len(gold), "submitted": len(subs), "matched": matched,
        "recall": round(recall, 4), "precision": round(precision, 4), "f1": round(f, 4),
        "resolution_accuracy": round(resolved_ok / n_res, 4) if n_res else None,
    }


SPEED_FAST_S, SPEED_SLOW_S = 2.0, 20.0   # p95 at/below FAST = 1.0, at/above SLOW = 0.0
WEIGHTS = {"accuracy": 0.55, "conflicts": 0.15, "consistency": 0.15, "speed": 0.15}


def _core(text: str) -> frozenset:
    """The 'gist' of an answer for consistency checks: its numeric values."""
    return frozenset(_numbers(text))


def score_consistency(results: list[dict], repeats: list[dict]) -> dict:
    """(a) paraphrase groups: every member right, or every member giving the same values;
       (b) repeat runs: the same question asked again yields the same values."""
    groups = defaultdict(list)
    for r in results:
        if r.get("group"):
            groups[r["group"]].append(r)
    g_ok = 0
    for members in groups.values():
        all_right = all(m["correctness"] >= 0.999 for m in members)
        same = len({_core(m["answer"]["text"]) for m in members}) == 1
        g_ok += 1 if (all_right or same) else 0
    by_id = {r["id"]: r for r in results}
    r_ok = sum(1 for rep in repeats if _core(rep["text"]) == _core(by_id[rep["id"]]["answer"]["text"]))
    para = g_ok / len(groups) if groups else None
    rep = r_ok / len(repeats) if repeats else None
    parts = [x for x in (para, rep) if x is not None]
    return {
        "paraphrase_groups": len(groups), "paraphrase_consistency": None if para is None else round(para, 4),
        "repeat_checks": len(repeats), "repeat_consistency": None if rep is None else round(rep, 4),
        "score": round(sum(parts) / len(parts), 4) if parts else None,
    }


def score_speed(latencies: list[float]) -> dict:
    if not latencies:
        return {"score": 0.0}
    xs = sorted(latencies)
    p = lambda q: xs[min(len(xs) - 1, int(q * len(xs)))]
    p95 = p(0.95)
    s = 1.0 if p95 <= SPEED_FAST_S else max(0.0, (SPEED_SLOW_S - p95) / (SPEED_SLOW_S - SPEED_FAST_S))
    return {"p50_s": round(p(0.5), 2), "p95_s": round(p95, 2), "max_s": round(xs[-1], 2), "score": round(s, 4)}


def summarize(results: list[dict], conflict_report: dict | None, repeats: list[dict] | None = None) -> dict:
    by = defaultdict(list)
    for r in results:
        by[r["category"]].append(r)
    wsum = sum(r["weight"] for r in results) or 1.0
    qa = sum(r["score"] * r["weight"] for r in results) / wsum
    report = {
        "qa_score": round(qa, 4),
        "questions": len(results),
        "stale_trap_hits": sum(1 for r in results if r["trap_hits"]),
        "errors": sum(1 for r in results if r.get("error")),
        "by_category": {
            k: {"n": len(v), "score": round(sum(x["score"] for x in v) / len(v), 4)} for k, v in sorted(by.items())
        },
    }
    cons = score_consistency(results, repeats or [])
    speed = score_speed([r["latency_s"] for r in results if not r.get("error")])
    report["consistency"], report["speed"] = cons, speed

    # Speed and consistency only count in proportion to accuracy: fast-and-wrong or
    # consistently-wrong must not win.
    pillars = {
        "accuracy": qa,
        "consistency": None if cons["score"] is None else cons["score"] * qa,
        "speed": speed["score"] * qa,
    }
    if conflict_report:
        report["conflicts"] = conflict_report
        pillars["conflicts"] = 0.7 * conflict_report["f1"] + 0.3 * (conflict_report["resolution_accuracy"] or 0)
    used = {k: WEIGHTS[k] for k, v in pillars.items() if v is not None}
    total_w = sum(used.values())
    report["pillars"] = {k: round(v, 4) for k, v in pillars.items() if v is not None}
    report["final_score"] = round(sum(pillars[k] * w for k, w in used.items()) / total_w, 4)
    return report
