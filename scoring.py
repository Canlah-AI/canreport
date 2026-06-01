"""Shared overall-score computation for canreport.

Single source of truth for the headline site-health score. Replaces the old
saturating subtractive formula (100 - p0*25 - p1*10 - p2*3), which floored any
thorough audit (12+ findings) at 0 even when most modules were healthy.

Model: weighted average of per-module health scores, with a bounded P0 cap
keyed on the COUNT OF AFFECTED MODULES (not raw finding count). P1/P2 severities
are already baked into each module's own score, so they are not double-counted
here.
"""

from numbers import Real


def compute_overall_score(modules: list[dict]) -> int:
    """Weighted avg of scored modules + bounded P0 cap. score==-1 = N/A (skip),
    overall_weight<=0 excludes non-diagnostic modules (roadmap). P1/P2 already
    in module scores; P0 caps by affected-module count, not finding count."""
    scored = []
    p0_modules = 0
    for mod in modules:
        raw = mod.get("score", -1)
        weight = float(mod.get("overall_weight", 1.0))
        if weight <= 0:
            continue
        if not isinstance(raw, Real) or raw < 0:
            continue
        score = max(0.0, min(100.0, float(raw)))
        scored.append((score, weight))
        if any((f.get("severity", "").upper() == "P0") for f in (mod.get("findings") or [])):
            p0_modules += 1
    if not scored:
        return -1
    base = sum(s * w for s, w in scored) / sum(w for _, w in scored)
    if p0_modules:
        p0_cap = max(55, 75 - 7 * (p0_modules - 1))
        base = min(base, p0_cap)
    return int(round(base))
