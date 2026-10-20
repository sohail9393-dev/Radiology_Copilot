"""
Streamlit UI for Radiology Copilot -- calls the FastAPI backend (xray_api.py)
rather than loading the model directly. Same client/server split as the
calculator project's Gradio-calls-FastAPI setup.

Setup:
    pip install streamlit requests pillow

Run (with xray_api.py already running via uvicorn in another terminal):
    streamlit run streamlit_app.py
"""

import os
import requests
import streamlit as st
from PIL import Image

# Set API_BASE_URL to point at a remote API (e.g. a deployed API address).
# Falls back to localhost so local development works unchanged.
API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
API_URL = f"{API_BASE_URL}/analyze-xray"

st.set_page_config(page_title="Radiology Copilot", page_icon=None, layout="wide")

st.title("Radiology Copilot")
st.caption(
    "Research/demo tool for exploring chest X-ray findings. "
    "Not a diagnostic device -- all output requires review by a licensed clinician."
)

uploaded_file = st.file_uploader(
    "Upload a chest X-ray (jpeg or png)", type=["jpeg", "jpg", "png"]
)

# A stored report belongs to the upload it was generated for. Without
# this, uploading a new X-ray still rendered the PREVIOUS study's report
# (and its "no issues" banner) beneath the new image.
if uploaded_file is not None:
    upload_signature = (uploaded_file.name, uploaded_file.size)
    if st.session_state.get("report_upload_signature") != upload_signature:
        st.session_state.pop("last_report_result", None)
        st.session_state.pop("last_report_study_id", None)
        st.session_state["report_upload_signature"] = upload_signature

if uploaded_file is not None:
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Uploaded image")
        image = Image.open(uploaded_file)
        st.image(image, use_container_width=True)

    with st.spinner("Running model inference..."):
        try:
            uploaded_file.seek(0)  # reset pointer after PIL read it above
            files = {"file": (uploaded_file.name, uploaded_file, uploaded_file.type)}
            response = requests.post(API_URL, files=files, timeout=30)
        except requests.RequestException as e:
            st.error(f"Could not reach the API at {API_URL}. Is it running? ({e})")
            st.stop()

    if response.status_code != 200:
        # Guard the error path: a non-JSON body (proxy 502, uvicorn 500)
        # made response.json() raise inside the handler itself.
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        st.error(f"API error: {detail or response.text}")
        st.stop()

    result = response.json()
    findings = result["findings"]
    study_id = result["study_id"]

    with col2:
        st.subheader("Findings")
        top_findings = dict(list(findings.items())[:5])
        st.bar_chart(top_findings)

        st.subheader("All findings")
        st.dataframe(
            {"Finding": list(findings.keys()), "Probability": list(findings.values())},
            use_container_width=True,
            hide_index=True,
        )

    st.warning(
        "These are raw model probabilities, not a diagnosis. "
        "A licensed clinician must review before any clinical use."
    )

    st.divider()
    st.subheader("Generate full report")
    st.caption(
        "Runs the full agent pipeline: Planner \u2192 Correlation \u2192 Evidence \u2192 Critic \u2192 Synthesis. "
        "This takes noticeably longer than the upload above -- several LLM calls happen in sequence."
    )

    symptoms_input = st.text_area(
        "Patient-reported symptoms (optional)",
        placeholder="e.g. Cough, fever, and shortness of breath for 3 days.",
    )

    if st.button("Generate Full Report"):
        with st.spinner("Running Planner \u2192 Correlation \u2192 Evidence \u2192 Critic \u2192 Synthesis... this can take 15-30+ seconds."):
            try:
                report_resp = requests.post(
                    f"{API_BASE_URL}/studies/{study_id}/report",
                    params={"patient_symptoms": symptoms_input},
                    timeout=120,
                )
            except requests.RequestException as e:
                st.error(f"Could not reach the API ({e})")
                st.stop()

        if report_resp.status_code != 200:
            st.error(f"API error: {report_resp.text}")
            st.stop()

        result = report_resp.json()
        st.session_state["last_report_result"] = result
        st.session_state["last_report_study_id"] = study_id

    # Render from session_state so it survives the review button's rerun below
    if "last_report_result" in st.session_state:
        result = st.session_state["last_report_result"]
        study_id_for_display = st.session_state["last_report_study_id"]

        if result.get("report") and result["report"].get("report_text"):
            status = result["report"]["status"]
            if status == "needs_revision":
                st.error("Critic flagged issues with this pipeline run -- see Caveats section below.")
            elif status == "pass":
                st.success("Critic reviewed this pipeline run and found no issues.")

            st.markdown(result["report"]["report_text"])

            st.download_button(
                label="Download Report (.md)",
                data=result["report"]["report_text"],
                file_name=f"radiology_report_study_{study_id_for_display}.md",
                mime="text/markdown",
            )

            st.divider()
            st.subheader("Clinician review")
            report_id = result.get("report_id")

            if report_id is None:
                st.warning("No report_id available -- this report wasn't saved to the database, so it can't be reviewed.")
            else:
                reviewer_name = st.text_input("Reviewer name", key="reviewer_name")
                decision = st.radio(
                    "Decision", ["approved", "edited", "rejected"], horizontal=True, key="review_decision"
                )
                notes = st.text_area("Notes (optional)", key="review_notes")

                if st.button("Submit Review"):
                    if not reviewer_name:
                        st.warning("Enter a reviewer name before submitting.")
                    else:
                        try:
                            review_resp = requests.post(
                                f"{API_BASE_URL}/reports/{report_id}/review",
                                params={
                                    "reviewer_name": reviewer_name,
                                    "decision": decision,
                                    "notes": notes,
                                },
                                timeout=10,
                            )
                            if review_resp.status_code == 200:
                                st.success(f"Review logged: {decision} by {reviewer_name}")
                            else:
                                st.error(f"Failed to log review: {review_resp.text}")
                        except requests.RequestException as e:
                            st.error(f"Could not reach the API ({e})")

            with st.expander("See intermediate agent outputs (Plan, Correlation, Evidence, Critique)"):
                st.json(
                    {
                        "plan": result["plan"],
                        "correlation": result["correlation"],
                        "evidence": result["evidence"],
                        "critique": result["critique"],
                    }
                )
        else:
            st.error(f"Report generation failed: {result.get('error', 'Unknown error')}")
else:
    st.info("Upload a chest X-ray image above to see findings.")