"""
Step 6c: Critic agent -- Groq version.

Reviews the Planner, Correlation, and Evidence agents' outputs TOGETHER,
checking for:
  - Contradictions between agents (e.g. Planner calls something noise,
    but Correlation says it's strongly consistent with symptoms)
  - Claims not actually backed by the retrieved evidence text
  - Findings that got dropped somewhere along the pipeline

This is a higher-level check than the schema/grounding validation already
built into each individual agent -- those catch "did this one agent's
output make sense on its own"; this catches "do these agents' outputs make
sense TOGETHER."

Setup:
    pip install groq python-dotenv

Add to your .env:
    GROQ_API_KEY=gsk_...
    CRITIC_MODEL=openai/gpt-oss-120b   (optional, this is the default)
"""

import json
import os

from dotenv import load_dotenv
from groq import Groq

from llm_utils import normalize_verdict, strip_code_fences

load_dotenv()

_client = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"), max_retries=5)
    return _client


CRITIC_SYSTEM_PROMPT = """You are a critic agent reviewing the combined \
output of a clinical decision-support pipeline: a Planner (decided which \
findings to prioritize), optionally a Correlation agent (assessed symptom \
consistency -- this may have been skipped if the Planner decided it wasn't \
needed, in which case correlation will show a "skipped" note, and that is \
NOT itself an issue to flag), and an Evidence agent (retrieved reference \
text). You do NOT diagnose. Your job is ONLY to check this output for \
problems, specifically:

1. CONTRADICTIONS: If correlation was actually run, does the Planner call \
something noise while Correlation treats it as clinically significant, or \
vice versa?
2. UNSUPPORTED CLAIMS: Does any reasoning make a specific clinical claim \
that isn't actually backed by the retrieved evidence text provided?
3. COMPLETENESS: Were any of the Planner's own priority findings silently \
missing from the evidence retrieved for them?

Note on evidence: each evidence item has "queried_for" (the finding it was
retrieved for) and "matched_pathology" (what the closest available
reference actually discusses -- these can differ if no exact match
exists). Only flag "incomplete" if a priority finding has NO evidence item
with a matching "queried_for" at all. If "matched_pathology" differs from
"queried_for", that is an imperfect-match limitation worth noting under
unsupported_claim if reasoning treats it as a direct match, but it is NOT
the same as missing evidence entirely.

Respond with ONLY a JSON object, no other text, no markdown fences, matching \
exactly this shape:
{
  "issues_found": [
    {"type": "contradiction" | "unsupported_claim" | "incomplete" | "other", "description": "<specific description of the issue>"}
  ],
  "verdict": "pass" | "needs_revision",
  "summary": "<one or two sentence overall assessment>"
}

If you find no issues, return an empty issues_found list and verdict "pass".
"""


def critique_pipeline(plan: dict, correlation: dict, evidence: list) -> dict:
    """
    plan: output of plan_next_steps()
    correlation: output of correlate_findings()
    evidence: list of results from retrieve_evidence() calls
    """
    user_message = (
        f"PLANNER OUTPUT:\n{json.dumps(plan, indent=2)}\n\n"
        f"CORRELATION OUTPUT:\n{json.dumps(correlation, indent=2)}\n\n"
        f"EVIDENCE RETRIEVED:\n{json.dumps(evidence, indent=2)}\n"
    )

    model = os.getenv("CRITIC_MODEL", "openai/gpt-oss-120b")

    try:
        response = _get_client().chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
            max_tokens=1500,
        )
    except Exception as e:
        return {"error": f"Could not reach Groq (model={model}). ({e})"}

    raw_text = strip_code_fences(response.choices[0].message.content or "")


    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return {"error": "Critic did not return valid JSON", "raw_response": raw_text}

    required_keys = {"issues_found", "verdict", "summary"}
    missing_keys = required_keys - parsed.keys()
    if missing_keys:
        return {
            "error": f"Critic response is missing required keys: {sorted(missing_keys)}",
            "raw_response": parsed,
        }

    # Pin the verdict to the exact contract xray_api/streamlit rely on.
    # Without this, "Pass" / "needs revision" / null reached the UI, which
    # only recognises "pass" and "needs_revision" -- so the report rendered
    # with no caveat banner.
    raw_verdict = parsed.get("verdict")
    parsed["verdict"] = normalize_verdict(parsed)
    if parsed["verdict"] != raw_verdict:
        parsed["verdict_normalized_from"] = raw_verdict

    return parsed


if __name__ == "__main__":
    # Runs the FULL pipeline (Planner -> Correlation -> Evidence -> Critic)
    # against the most recent real uploaded study -- no hardcoded data.
    from database import list_studies, get_study
    from planner_agent_groq import plan_next_steps
    from correlation_agent_groq import correlate_findings
    from evidence_agent import retrieve_evidence

    studies = list_studies()
    if not studies:
        print("No studies found. Upload an X-ray through /analyze-xray first.")
    else:
        latest = studies[0]
        study = get_study(latest["id"])
        findings = {f["pathology"]: f["probability"] for f in study["findings"]}

        symptoms = "Cough, fever, and shortness of breath for 3 days."
        print(f"Running full pipeline on study {latest['id']} ({study['filename']})...\n")

        plan = plan_next_steps(findings, patient_symptoms=symptoms)
        print("PLAN:", json.dumps(plan, indent=2), "\n")

        if "error" in plan:
            print("Planner failed -- stopping here, nothing downstream is trustworthy.")
        else:
            priority = {item["pathology"]: item["probability"] for item in plan.get("priority_findings", [])}

            correlation = None
            if plan.get("requires_correlation_with_symptoms"):
                correlation = correlate_findings(priority, patient_symptoms=symptoms)
            else:
                correlation = {"skipped": "Planner determined correlation was not required."}
            print("CORRELATION:", json.dumps(correlation, indent=2), "\n")

            evidence = []
            for pathology in priority:
                evidence.extend(retrieve_evidence(pathology, top_k=1))
            print("EVIDENCE:", json.dumps(evidence, indent=2), "\n")

            critique = critique_pipeline(plan, correlation, evidence)
            print("CRITIQUE:", json.dumps(critique, indent=2))