"""
Step 6a: Planner agent -- Groq version.

Same job as the old Ollama planner (decide which findings need further
review), but calls Groq instead of a local Ollama server, so there is no
extra model server to deploy.

The model's output is validated before it is returned (all required keys
present, correct types, only pathology names that were actually in the
input). If validation fails, the model is asked to correct itself, up to
MAX_ATTEMPTS times; if it still fails, an {"error": ...} dict is returned
so xray_api.py stops the pipeline instead of running on a broken plan.

Setup:
    pip install groq python-dotenv

Add to your .env:
    GROQ_API_KEY=gsk_...
    PLANNER_MODEL=openai/gpt-oss-120b   (optional, this is the default)
"""

import json
import os

from dotenv import load_dotenv
from groq import Groq

from llm_utils import strip_code_fences

load_dotenv()

MAX_ATTEMPTS = 3
REQUIRED_KEYS = {
    "priority_findings",
    "likely_noise",
    "requires_correlation_with_symptoms",
    "notes",
}

_client = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"), max_retries=5)
    return _client


PLANNER_SYSTEM_PROMPT = """You are a clinical triage planner reviewing chest \
X-ray CV model findings before they go to further review agents. You do NOT \
diagnose. Your only job is to decide which findings are worth pursuing \
further (evidence lookup, correlation with patient symptoms) versus which \
are likely noise, given their probability and clinical plausibility.

Respond with ONLY a JSON object, no other text, no markdown fences, matching \
exactly this shape. ALL FOUR keys are REQUIRED in every response:
{
  "priority_findings": [
    {"pathology": "<n>", "probability": <number>, "reasoning": "<short reason this is worth pursuing>"}
  ],
  "likely_noise": ["<pathology names not worth pursuing further>"],
  "requires_correlation_with_symptoms": <true or false>,
  "notes": "<one or two sentence overall summary of the plan>"
}
"""


def _validate_plan(plan, findings: dict):
    """Return a string describing what is wrong with the plan, or None if OK."""
    if not isinstance(plan, dict):
        return "the response was not a JSON object"

    missing = REQUIRED_KEYS - plan.keys()
    if missing:
        return f"missing required keys: {sorted(missing)}"

    if not isinstance(plan["priority_findings"], list):
        return '"priority_findings" must be a list'
    if not isinstance(plan["likely_noise"], list):
        return '"likely_noise" must be a list'
    if not isinstance(plan["requires_correlation_with_symptoms"], bool):
        return '"requires_correlation_with_symptoms" must be true or false'

    # Grounding check: only pathology names from the input are allowed.
    allowed = set(findings.keys())
    for item in plan["priority_findings"]:
        if not isinstance(item, dict) or "pathology" not in item:
            return 'each "priority_findings" item must be an object with a "pathology" key'
    invented = {i["pathology"] for i in plan["priority_findings"]} - allowed
    invented |= {n for n in plan["likely_noise"] if isinstance(n, str)} - allowed
    if invented:
        return f"it named pathologies that were not in the input: {sorted(invented)}"

    return None


def plan_next_steps(findings: dict, patient_symptoms: str = "") -> dict:
    """
    findings: dict of {pathology: probability}, as returned by the CV model.
    patient_symptoms: optional free-text patient-reported symptoms.
    """
    model = os.getenv("PLANNER_MODEL", "openai/gpt-oss-120b")

    findings_text = "\n".join(f"- {k}: {v:.3f}" for k, v in findings.items())
    user_message = f"CV model findings:\n{findings_text}\n\n"
    user_message += (
        f"Patient-reported symptoms:\n{patient_symptoms}\n"
        if patient_symptoms
        else "No patient-reported symptoms were provided.\n"
    )

    messages = [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    last_problem = "unknown"
    last_raw = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = _get_client().chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                max_tokens=1500,
            )
        except Exception as e:
            # Same contract as the old Ollama version: return {"error": ...}
            # so xray_api.py's `if "error" in plan` check still works.
            return {"error": f"Could not reach Groq (model={model}). ({e})"}

        raw_text = (response.choices[0].message.content or "").strip()
        last_raw = raw_text

        # Defensive: strip markdown fences if the model adds them anyway.
        # (lstrip("json") here was a character-set strip, not a prefix strip.)
        raw_text = strip_code_fences(raw_text)

        try:
            plan = json.loads(raw_text)
            problem = _validate_plan(plan, findings)
        except json.JSONDecodeError:
            plan, problem = None, "the response was not valid JSON"

        if problem is None:
            return plan

        last_problem = problem
        # Ask the model to fix its own answer on the next attempt.
        messages = messages + [
            {"role": "assistant", "content": raw_text},
            {
                "role": "user",
                "content": (
                    f"Your previous response was rejected: {problem}. "
                    "Return the COMPLETE JSON object again with all four keys "
                    "(priority_findings, likely_noise, "
                    "requires_correlation_with_symptoms, notes), using only "
                    "pathology names from the findings list."
                ),
            },
        ]

    return {
        "error": f"Planner returned an invalid plan after {MAX_ATTEMPTS} attempts: {last_problem}",
        "raw_response": last_raw,
    }


if __name__ == "__main__":
    # Runs the planner against the MOST RECENT real uploaded study in the
    # database -- no hardcoded test data. Upload at least one X-ray through
    # /analyze-xray first, or this will find nothing.
    from database import list_studies, get_study

    studies = list_studies()
    if not studies:
        print("No studies found in the database yet. Upload an X-ray through "
              "/analyze-xray first, then run this again.")
    else:
        latest_study_id = studies[0]["id"]  # list_studies() is ordered newest first
        study = get_study(latest_study_id)
        findings = {f["pathology"]: f["probability"] for f in study["findings"]}

        print(f"Running planner on study {latest_study_id} ({study['filename']})...\n")
        plan = plan_next_steps(findings)
        print(json.dumps(plan, indent=2))
