"""
Step 6b: Correlation agent -- Groq version.

Same job as correlation_agent.py / correlation_agent_ollama.py, using Groq.
Includes the same hard grounding check: rejects any response that invents
pathology names not present in the actual input findings.

Setup:
    pip install groq python-dotenv

Add to your .env:
    GROQ_API_KEY=gsk_...
    CORRELATION_MODEL=openai/gpt-oss-120b
"""

import json
import os

from dotenv import load_dotenv
from groq import Groq

from llm_utils import strip_code_fences

load_dotenv()

_client = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"), max_retries=5)
    return _client


CORRELATION_SYSTEM_PROMPT = """You are a clinical correlation agent. You are \
given chest X-ray CV model findings and patient-reported symptoms. You do \
NOT diagnose. Your only job is to assess, for each finding, whether the \
patient's reported symptoms are consistent with, partially consistent with, \
or not clearly related to that finding -- based on general clinical \
knowledge of what symptoms typically accompany each condition.

CRITICAL: You MUST use the EXACT pathology names and EXACT probability \
values given to you in the findings list below. Do NOT invent, rename, \
substitute, or add any pathology that is not in the given list. Do NOT \
generate your own differential diagnosis from the symptoms. Your job is \
ONLY to assess the findings you were given -- one output entry per input \
finding, using its exact name and exact probability, no more and no fewer.

If no patient symptoms were provided, mark every finding's symptom_consistency \
as "no symptoms provided" and do not speculate.

Respond with ONLY a JSON object, no other text, no markdown fences, matching \
exactly this shape:
{
  "correlations": [
    {
      "pathology": "<EXACT name from the input findings, unchanged>",
      "probability": <EXACT probability from the input findings, unchanged>,
      "symptom_consistency": "consistent" | "partially consistent" | "not clearly related" | "no symptoms provided",
      "reasoning": "<one sentence explaining the assessment>"
    }
  ],
  "overall_assessment": "<one or two sentence summary of how well the overall symptom picture matches the findings>"
}
"""


def correlate_findings(findings: dict, patient_symptoms: str = "") -> dict:
    model = os.getenv("CORRELATION_MODEL", "openai/gpt-oss-120b")

    findings_text = "\n".join(f"- {k}: {v:.3f}" for k, v in findings.items())
    user_message = f"Findings to assess:\n{findings_text}\n\n"
    user_message += (
        f"Patient-reported symptoms:\n{patient_symptoms}\n"
        if patient_symptoms
        else "No patient-reported symptoms were provided.\n"
    )

    # Build the client inside the guard: a missing/invalid GROQ_API_KEY
    # raises at construction and would otherwise escape as an HTTP 500.
    try:
        client = _get_client()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CORRELATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
            max_tokens=1000,
        )
    except Exception as e:
        # Return an error dict (like the Planner/Critic) instead of letting
        # the exception become an HTTP 500 -- e.g. on a Groq rate limit.
        return {"error": f"Could not reach Groq (model={model}). ({e})"}

    raw_text = strip_code_fences(response.choices[0].message.content or "")

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return {"error": "Correlation agent did not return valid JSON", "raw_response": raw_text}

    # Hard grounding check -- same as the other two versions. Don't trust
    # the prompt alone; verify every returned pathology actually came from
    # the input.
    input_pathologies = set(findings.keys())
    correlations = parsed.get("correlations")
    if not isinstance(correlations, list) or not correlations:
        return {
            "error": (
                "Correlation response contained no correlations list. "
                f"raw_response={parsed!r}"
            ),
            "raw_response": parsed,
        }

    returned_pathologies = {c.get("pathology") for c in correlations}
    hallucinated = returned_pathologies - input_pathologies

    if hallucinated:
        return {
            "error": (
                f"Correlation agent invented pathologies not in the input: {sorted(hallucinated)}. "
                f"Input findings were: {sorted(input_pathologies)}. Rejecting this response."
            ),
            "raw_response": parsed,
        }

    # The prompt demands one entry per input finding ("no more and no fewer");
    # only the "no more" half used to be enforced, so a truncated response
    # was accepted as complete.
    missing = input_pathologies - returned_pathologies
    if missing:
        return {
            "error": (
                f"Correlation response omitted input findings: {sorted(missing)}. "
                f"Input findings were: {sorted(input_pathologies)}. Rejecting this response."
            ),
            "raw_response": parsed,
        }

    return parsed


if __name__ == "__main__":
    from database import list_studies, get_study

    studies = list_studies()
    if not studies:
        print("No studies found. Upload an X-ray through /analyze-xray first.")
    else:
        latest = studies[0]
        study = get_study(latest["id"])
        top_findings = {f["pathology"]: f["probability"] for f in study["findings"][:5]}

        print(f"Running correlation agent on study {latest['id']} ({study['filename']})...\n")
        result = correlate_findings(
            top_findings,
            patient_symptoms="Cough, fever, and shortness of breath for 3 days.",
        )
        print(json.dumps(result, indent=2))
