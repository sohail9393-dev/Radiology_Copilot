"""
Step 3: expose the pretrained CV model as an upload endpoint.

Setup (same venv as before):
    pip install fastapi uvicorn python-multipart

Run:
    uvicorn xray_api:app --reload

Then open http://127.0.0.1:8000/docs and use "Try it out" on POST /analyze-xray
to upload a real image file and see the findings returned as JSON.
"""

import io

import skimage.io
import torch
import torchvision.transforms as transforms
import torchxrayvision as xrv
from fastapi import FastAPI, File, HTTPException, UploadFile

from database import (
    init_db, log_study, get_study, log_evidence, init_evidence_table,
    log_report, init_report_table, log_clinician_review,
    init_clinician_review_table, get_reviews_for_report,
)
from evidence_agent import retrieve_evidence
from planner_agent_groq import plan_next_steps
from correlation_agent_groq import correlate_findings
from critic_agent import critique_pipeline
from synthesis_agent import generate_report

app = FastAPI(title="Radiology Copilot - CV Inference API")

# Load the model once at startup, not on every request -- loading is slow,
# inference on an already-loaded model is fast.
_model = None


@app.on_event("startup")
def load_model():
    global _model
    _model = xrv.models.DenseNet(weights="densenet121-res224-all")
    _model.eval()
    init_db()  # make sure xray_studies / cv_findings tables exist
    init_evidence_table()  # make sure evidence_citations table exists
    init_report_table()  # make sure agent_reports table exists
    init_clinician_review_table()  # make sure clinician_reviews table exists


@app.get("/health")
def health():
    """Liveness/readiness probe for the load balancer and container orchestrator."""
    return {"status": "ok", "model_loaded": _model is not None}


def preprocess_image_bytes(image_bytes: bytes) -> torch.Tensor:
    """Same preprocessing as the notebook, but reading from uploaded bytes
    instead of a file path on disk."""
    img = skimage.io.imread(io.BytesIO(image_bytes))

    img = xrv.datasets.normalize(img, 255)
    if img.ndim == 3:
        img = img.mean(axis=2)
    img = img[None, :, :]

    transform = transforms.Compose(
        [xrv.datasets.XRayCenterCrop(), xrv.datasets.XRayResizer(224)]
    )
    img = transform(img)
    return torch.from_numpy(img).unsqueeze(0).float()


@app.post("/analyze-xray")
async def analyze_xray(file: UploadFile = File(...)):
    """Upload a chest X-ray image, get back findings sorted by probability."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image (jpeg/png).")

    image_bytes = await file.read()

    try:
        image_tensor = preprocess_image_bytes(image_bytes)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read image: {e}")

    with torch.no_grad():
        outputs = _model(image_tensor)

    probabilities = outputs[0].detach().numpy()
    findings = {k: float(v) for k, v in zip(_model.pathologies, probabilities) if k}
    findings = dict(sorted(findings.items(), key=lambda kv: kv[1], reverse=True))

    study_id = log_study(file.filename, findings)

    return {"study_id": study_id, "filename": file.filename, "findings": findings}


@app.post("/studies/{study_id}/evidence")
def run_evidence_agent(study_id: int, threshold: float = 0.5, top_findings: int = 3):
    """
    Evidence/RAG agent: for a study's strongest findings (above threshold),
    retrieve relevant reference passages and log them.
    """
    study = get_study(study_id)
    if study is None:
        raise HTTPException(status_code=404, detail=f"No study found with id {study_id}")

    # findings are already sorted by probability descending (see get_study)
    strong_findings = [f for f in study["findings"] if f["probability"] >= threshold][:top_findings]

    evidence_results = []
    for finding in strong_findings:
        pathology = finding["pathology"]
        matches = retrieve_evidence(pathology, top_k=1)
        for match in matches:
            log_evidence(study_id, pathology, match["text"], match["source"])
            evidence_results.append(
                {
                    "pathology": pathology,
                    "probability": finding["probability"],
                    "matched_reference": match["matched_pathology"],
                    "similarity": match["similarity"],
                    "text": match["text"],
                    "source": match["source"],
                }
            )

    return {"study_id": study_id, "evidence": evidence_results}


@app.post("/studies/{study_id}/plan")
def run_planner_agent(study_id: int, patient_symptoms: str = ""):
    """
    Planner agent: decide which findings are worth pursuing further versus
    likely noise, given this study's CV findings and optional patient
    symptoms.
    """
    study = get_study(study_id)
    if study is None:
        raise HTTPException(status_code=404, detail=f"No study found with id {study_id}")

    findings = {f["pathology"]: f["probability"] for f in study["findings"]}
    plan = plan_next_steps(findings, patient_symptoms=patient_symptoms)

    return {"study_id": study_id, "plan": plan}


@app.post("/studies/{study_id}/correlate")
def run_correlation_agent(study_id: int, patient_symptoms: str = "", threshold: float = 0.5, top_findings: int = 5):
    """
    Correlation agent: assess whether patient-reported symptoms are
    consistent with this study's strongest findings.
    """
    study = get_study(study_id)
    if study is None:
        raise HTTPException(status_code=404, detail=f"No study found with id {study_id}")

    strong_findings = {
        f["pathology"]: f["probability"]
        for f in study["findings"]
        if f["probability"] >= threshold
    }
    strong_findings = dict(list(strong_findings.items())[:top_findings])

    result = correlate_findings(strong_findings, patient_symptoms=patient_symptoms)

    return {"study_id": study_id, "correlation": result}


@app.post("/studies/{study_id}/critique")
def run_critic_agent(study_id: int, patient_symptoms: str = ""):
    """
    Runs Planner first. The Planner's OWN output then drives what happens
    next:
      - Correlation only runs if the Planner said requires_correlation_with_symptoms
        is true AND patient symptoms were actually provided.
      - Evidence retrieval runs on the Planner's priority_findings specifically,
        not an independently recomputed threshold list.
    Finally hands everything to the Critic for cross-checking.
    """
    study = get_study(study_id)
    if study is None:
        raise HTTPException(status_code=404, detail=f"No study found with id {study_id}")

    findings = {f["pathology"]: f["probability"] for f in study["findings"]}
    plan = plan_next_steps(findings, patient_symptoms=patient_symptoms)

    if "error" in plan:
        # Planner itself failed -- nothing downstream can trust its output,
        # so stop here rather than feeding a broken plan to other agents.
        return {"study_id": study_id, "plan": plan, "correlation": None, "evidence": [], "critique": None}

    priority_findings = {
        item["pathology"]: item["probability"] for item in plan.get("priority_findings", [])
    }

    # The Planner's own decision controls whether Correlation runs at all.
    correlation = None
    if plan.get("requires_correlation_with_symptoms") and patient_symptoms:
        correlation = correlate_findings(priority_findings, patient_symptoms=patient_symptoms)
    elif plan.get("requires_correlation_with_symptoms") and not patient_symptoms:
        correlation = {"skipped": "Planner requested correlation, but no patient_symptoms were provided."}
    else:
        correlation = {"skipped": "Planner determined correlation with symptoms was not required."}

    # Evidence retrieval runs on the Planner's priority findings, not an
    # independently recomputed list -- this is what makes the plan's output
    # actually drive the pipeline instead of being informational only.
    evidence = []
    for pathology in priority_findings:
        matches = retrieve_evidence(pathology, top_k=1)
        for match in matches:
            log_evidence(study_id, match["queried_for"], match["text"], match["source"])
        evidence.extend(matches)

    critique = critique_pipeline(plan, correlation, evidence)

    return {
        "study_id": study_id,
        "plan": plan,
        "correlation": correlation,
        "evidence": evidence,
        "critique": critique,
    }


@app.post("/studies/{study_id}/report")
def generate_full_report(study_id: int, patient_symptoms: str = ""):
    """
    Runs the ENTIRE pipeline: Planner -> Correlation (if needed) -> Evidence
    -> Critic -> Synthesis. Logs the final report to agent_reports and
    returns it, along with every intermediate stage for transparency.
    """
    study = get_study(study_id)
    if study is None:
        raise HTTPException(status_code=404, detail=f"No study found with id {study_id}")

    findings = {f["pathology"]: f["probability"] for f in study["findings"]}
    plan = plan_next_steps(findings, patient_symptoms=patient_symptoms)

    if "error" in plan:
        return {"study_id": study_id, "plan": plan, "report": None, "error": "Planner failed; cannot proceed."}

    priority_findings = {
        item["pathology"]: item["probability"] for item in plan.get("priority_findings", [])
    }

    if plan.get("requires_correlation_with_symptoms") and patient_symptoms:
        correlation = correlate_findings(priority_findings, patient_symptoms=patient_symptoms)
    elif plan.get("requires_correlation_with_symptoms") and not patient_symptoms:
        correlation = {"skipped": "Planner requested correlation, but no patient_symptoms were provided."}
    else:
        correlation = {"skipped": "Planner determined correlation with symptoms was not required."}

    evidence = []
    for pathology in priority_findings:
        matches = retrieve_evidence(pathology, top_k=1)
        for match in matches:
            log_evidence(study_id, match["queried_for"], match["text"], match["source"])
        evidence.extend(matches)

    critique = critique_pipeline(plan, correlation, evidence)
    report = generate_report(study_id, study["filename"], plan, correlation, evidence, critique)

    if report["report_text"]:
        report_id = log_report(study_id, report["report_text"], report["status"])
    else:
        report_id = None

    return {
        "study_id": study_id,
        "report_id": report_id,
        "plan": plan,
        "correlation": correlation,
        "evidence": evidence,
        "critique": critique,
        "report": report,
        "error": report.get("error"),  # shown by the UI when no report was produced
    }


@app.post("/reports/{report_id}/review")
def submit_clinician_review(report_id: int, reviewer_name: str, decision: str, notes: str = ""):
    """
    Log a clinician's review decision for a generated report. This is the
    step that makes the system honestly "decision support" rather than
    autonomous -- nothing here changes clinical care until a human records
    a real decision here.
    """
    if decision not in ("approved", "edited", "rejected"):
        raise HTTPException(
            status_code=400,
            detail="decision must be one of: approved, edited, rejected",
        )

    review_id = log_clinician_review(report_id, reviewer_name, decision, notes)
    return {"review_id": review_id, "report_id": report_id, "decision": decision}


@app.get("/reports/{report_id}/reviews")
def list_reviews_for_report(report_id: int):
    """Return the review history for a given report."""
    reviews = get_reviews_for_report(report_id)
    return {"report_id": report_id, "reviews": reviews}