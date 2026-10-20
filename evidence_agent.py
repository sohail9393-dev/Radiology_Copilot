"""
Step 5: Evidence/RAG agent.

Given a pathology finding, retrieves the most relevant reference text from a
knowledge base using semantic similarity search (real sentence embeddings,
not exact string matching).

IMPORTANT: KNOWLEDGE_BASE below is a small, illustrative starter set written
for demo/portfolio purposes -- general textbook-level facts, NOT a curated
clinical literature database. Do not treat this as a source of clinical
truth. A real system would replace this with embedded, current, curated
medical sources (e.g. clinical guidelines, peer-reviewed literature).

Setup:
    pip install sentence-transformers numpy
"""

import numpy as np
from sentence_transformers import SentenceTransformer

KNOWLEDGE_BASE = [
    {
        "pathology": "Pneumonia",
        "text": "Pneumonia is an infection that inflames the air sacs in one "
        "or both lungs, which may fill with fluid or pus. On a chest X-ray "
        "it typically appears as an area of increased opacity or "
        "consolidation, often localized to a specific lobe.",
        "source": "General clinical reference (illustrative)",
    },
    {
        "pathology": "Effusion",
        "text": "Pleural effusion is an abnormal accumulation of fluid in "
        "the pleural space surrounding the lungs. On X-ray it often appears "
        "as blunting of the costophrenic angle or a meniscus-shaped opacity "
        "at the lung base.",
        "source": "General clinical reference (illustrative)",
    },
    {
        "pathology": "Atelectasis",
        "text": "Atelectasis refers to partial or complete collapse of lung "
        "tissue, reducing the amount of air in the affected area. On "
        "imaging it can appear as volume loss or increased density, and is "
        "common postoperatively or with airway obstruction.",
        "source": "General clinical reference (illustrative)",
    },
    {
        "pathology": "Cardiomegaly",
        "text": "Cardiomegaly describes an enlarged heart, often assessed "
        "on a chest X-ray using the cardiothoracic ratio -- the width of "
        "the heart compared to the width of the chest cavity.",
        "source": "General clinical reference (illustrative)",
    },
    {
        "pathology": "Consolidation",
        "text": "Consolidation occurs when air in the lungs is replaced by "
        "fluid, pus, or other material, causing lung tissue to become "
        "firm. It appears as a dense, opaque area on X-ray and is commonly "
        "associated with pneumonia.",
        "source": "General clinical reference (illustrative)",
    },
    {
        "pathology": "Pneumothorax",
        "text": "Pneumothorax is the presence of air in the pleural space, "
        "which can cause partial or complete lung collapse. On X-ray it "
        "appears as a visible pleural line with absence of lung markings "
        "beyond it.",
        "source": "General clinical reference (illustrative)",
    },
    {
        "pathology": "Edema",
        "text": "Pulmonary edema refers to fluid accumulation in lung "
        "tissue and air spaces, often due to heart failure. On X-ray it "
        "can present with increased vascular markings and a hazy pattern "
        "of opacity.",
        "source": "General clinical reference (illustrative)",
    },
    {
        "pathology": "Nodule",
        "text": "A pulmonary nodule is a small, round area of increased "
        "density in the lung, generally under 3 centimeters. Nodules can "
        "be benign or malignant, and follow-up imaging is often used to "
        "characterize them.",
        "source": "General clinical reference (illustrative)",
    },
    {
        "pathology": "Mass",
        "text": "A pulmonary mass is similar to a nodule but larger, "
        "generally over 3 centimeters. Masses carry a higher relative "
        "likelihood of malignancy than smaller nodules and typically "
        "warrant further workup.",
        "source": "General clinical reference (illustrative)",
    },
    {
        "pathology": "Fibrosis",
        "text": "Pulmonary fibrosis involves scarring of lung tissue, which "
        "can produce a reticular, net-like pattern on imaging. It "
        "progressively reduces lung elasticity over time.",
        "source": "General clinical reference (illustrative)",
    },
    {
        "pathology": "Emphysema",
        "text": "Emphysema is a form of chronic obstructive pulmonary "
        "disease in which lung air sacs are damaged and enlarged. On X-ray "
        "it can appear as hyperinflated lungs and flattened diaphragms.",
        "source": "General clinical reference (illustrative)",
    },
    {
        "pathology": "Lung Opacity",
        "text": "Lung opacity is a general term for any area of the lung "
        "that appears denser than expected on X-ray, which can indicate "
        "fluid, infection, collapse, or a mass. It is nonspecific and "
        "typically requires correlation with other findings.",
        "source": "General clinical reference (illustrative)",
    },
]

_model = None
_corpus_embeddings = None


def _get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def _get_corpus_embeddings():
    """Embed the knowledge base once and cache it -- avoids recomputing
    embeddings for the same fixed corpus on every single call."""
    global _corpus_embeddings
    if _corpus_embeddings is None:
        model = _get_model()
        texts = [entry["text"] for entry in KNOWLEDGE_BASE]
        _corpus_embeddings = model.encode(texts)
    return _corpus_embeddings


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    a_norm = a / np.linalg.norm(a, axis=-1, keepdims=True)
    b_norm = b / np.linalg.norm(b, axis=-1, keepdims=True)
    return a_norm @ b_norm.T


def retrieve_evidence(pathology: str, top_k: int = 1) -> list:
    """
    Retrieve the most relevant reference passage(s) for a pathology finding,
    using semantic similarity -- this means it can find something
    reasonable even if the query phrasing doesn't exactly match a corpus
    entry's label.

    IMPORTANT: "queried_for" is what you searched for. "matched_pathology"
    is what the closest entry in the knowledge base is actually about --
    these can differ (e.g. querying "Mass" might match the "Nodule" entry,
    since they're described very similarly). Always check both, and don't
    treat a low-similarity, differently-labeled match as "no evidence" --
    it's an imperfect match, which is different from nothing at all.
    """
    model = _get_model()
    query_embedding = model.encode([f"chest X-ray finding: {pathology}"])
    corpus_embeddings = _get_corpus_embeddings()

    similarities = _cosine_similarity(query_embedding, corpus_embeddings)[0]
    top_indices = np.argsort(similarities)[::-1][:top_k]

    return [
        {
            "queried_for": pathology,
            "matched_pathology": KNOWLEDGE_BASE[i]["pathology"],
            "text": KNOWLEDGE_BASE[i]["text"],
            "source": KNOWLEDGE_BASE[i]["source"],
            "similarity": float(similarities[i]),
        }
        for i in top_indices
    ]


if __name__ == "__main__":
    for finding in ["Lung Opacity", "Pneumonia", "Effusion", "Mass", "Fracture"]:
        print(f"\nQuery: {finding}")
        for r in retrieve_evidence(finding, top_k=1):
            match_note = (
                f"(exact match)" if r["matched_pathology"] == finding
                else f"(closest match is actually '{r['matched_pathology']}', not an exact entry for '{finding}')"
            )
            print(f"  -> similarity {r['similarity']:.2f} {match_note}")
            print(f"     {r['text'][:100]}...")