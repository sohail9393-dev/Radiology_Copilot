"""Small shared helpers for the Groq-backed agents.

Kept in one place so the Planner, Correlation, Critic and Synthesis agents
cannot drift apart on how they clean model output or interpret a verdict.
"""


def strip_code_fences(text: str) -> str:
    """Remove markdown code fences and any language tag from model output.

    Note: str.lstrip(chars) strips a *character set*, not a prefix -- using
    lstrip("json") would eat leading j/o/s/n characters from real JSON.
    """
    text = text.strip()
    if not text.startswith("```"):
        return text
    body = text[3:]
    newline = body.find("\n")
    if newline != -1 and body[:newline].strip().lower() in ("json", "javascript", ""):
        body = body[newline + 1:]
    return body.rstrip().removesuffix("```").strip()


def normalize_verdict(critique: dict) -> str:
    """Map whatever the critic said onto the exact contract the UI relies on.

    `streamlit_app.py` only recognises "pass" and "needs_revision". Anything
    unrecognised previously fell through and rendered the report with NO
    caveat banner -- so fail safe: treat any doubt as revision required.
    """
    verdict = str((critique or {}).get("verdict") or "").strip().lower()
    if verdict in ("pass", "passed", "ok", "clean", "no_issues", "none"):
        return "pass"
    return "needs_revision"
