"""
Step 5 (revised): Evidence/RAG agent, now backed by ChromaDB.

Same retrieve_evidence() interface as before, but the knowledge base is now
a real persistent vector store instead of an in-memory Python list -- you
can add new reference documents over time via add_knowledge(), and they
persist across restarts in ./chroma_store.

IMPORTANT: the seed knowledge base below is still a small, illustrative
starter set -- general textbook-level facts, NOT a curated clinical
literature database. The point of this change is the STORAGE mechanism
(a real vector DB you can grow), not a claim that the content is now
clinically authoritative.

Setup:
    pip install chromadb sentence-transformers
"""

import hashlib

import chromadb
from chromadb.utils import embedding_functions

CHROMA_PATH = "./chroma_store"
COLLECTION_NAME = "medical_knowledge_base"

SEED_KNOWLEDGE_BASE = [
    {"pathology": "Pneumonia", "text": "Pneumonia is an infection that inflames the air sacs in one or both lungs, which may fill with fluid or pus. On a chest X-ray it typically appears as an area of increased opacity or consolidation, often localized to a specific lobe.", "source": "General clinical reference (illustrative)"},
    {"pathology": "Effusion", "text": "Pleural effusion is an abnormal accumulation of fluid in the pleural space surrounding the lungs. On X-ray it often appears as blunting of the costophrenic angle or a meniscus-shaped opacity at the lung base.", "source": "General clinical reference (illustrative)"},
    {"pathology": "Atelectasis", "text": "Atelectasis refers to partial or complete collapse of lung tissue, reducing the amount of air in the affected area. On imaging it can appear as volume loss or increased density, and is common postoperatively or with airway obstruction.", "source": "General clinical reference (illustrative)"},
    {"pathology": "Cardiomegaly", "text": "Cardiomegaly describes an enlarged heart, often assessed on a chest X-ray using the cardiothoracic ratio -- the width of the heart compared to the width of the chest cavity.", "source": "General clinical reference (illustrative)"},
    {"pathology": "Consolidation", "text": "Consolidation occurs when air in the lungs is replaced by fluid, pus, or other material, causing lung tissue to become firm. It appears as a dense, opaque area on X-ray and is commonly associated with pneumonia.", "source": "General clinical reference (illustrative)"},
    {"pathology": "Pneumothorax", "text": "Pneumothorax is the presence of air in the pleural space, which can cause partial or complete lung collapse. On X-ray it appears as a visible pleural line with absence of lung markings beyond it.", "source": "General clinical reference (illustrative)"},
    {"pathology": "Edema", "text": "Pulmonary edema refers to fluid accumulation in lung tissue and air spaces, often due to heart failure. On X-ray it can present with increased vascular markings and a hazy pattern of opacity.", "source": "General clinical reference (illustrative)"},
    {"pathology": "Nodule", "text": "A pulmonary nodule is a small, round area of increased density in the lung, generally under 3 centimeters. Nodules can be benign or malignant, and follow-up imaging is often used to characterize them.", "source": "General clinical reference (illustrative)"},
    {"pathology": "Mass", "text": "A pulmonary mass is similar to a nodule but larger, generally over 3 centimeters. Masses carry a higher relative likelihood of malignancy than smaller nodules and typically warrant further workup.", "source": "General clinical reference (illustrative)"},
    {"pathology": "Fibrosis", "text": "Pulmonary fibrosis involves scarring of lung tissue, which can produce a reticular, net-like pattern on imaging. It progressively reduces lung elasticity over time.", "source": "General clinical reference (illustrative)"},
    {"pathology": "Emphysema", "text": "Emphysema is a form of chronic obstructive pulmonary disease in which lung air sacs are damaged and enlarged. On X-ray it can appear as hyperinflated lungs and flattened diaphragms.", "source": "General clinical reference (illustrative)"},
    {"pathology": "Lung Opacity", "text": "Lung opacity is a general term for any area of the lung that appears denser than expected on X-ray, which can indicate fluid, infection, collapse, or a mass. It is nonspecific and typically requires correlation with other findings.", "source": "General clinical reference (illustrative)"},
]

_client = None
_collection = None


def _get_collection():
    """Connect to the persistent Chroma store, seeding it on first run only."""
    global _client, _collection
    if _collection is not None:
        return _collection

    _client = chromadb.PersistentClient(path=CHROMA_PATH)
    embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )
    _collection = _client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=embedding_fn,
        metadata={"hnsw:space": "cosine"},
    )

    # Idempotent seeding. The old `if count() == 0` guard re-seeded whole
    # copies on any re-run, and ingest_knowledge.py then added the same
    # documents again under different ids (16 embeddings for 12 docs).
    # upsert() with content-derived ids makes re-runs safe.
    existing_ids = set(_collection.get(include=[])["ids"])
    pending_docs, pending_meta, pending_ids = [], [], []
    for i, entry in enumerate(SEED_KNOWLEDGE_BASE):
        seed_id = f"seed_{i}"
        if seed_id in existing_ids:
            continue
        pending_docs.append(entry["text"])
        pending_meta.append(
            {"pathology": entry["pathology"], "source": entry["source"]}
        )
        pending_ids.append(seed_id)
    if pending_ids:
        _collection.upsert(
            documents=pending_docs,
            metadatas=pending_meta,
            ids=pending_ids,
        )

    return _collection


def add_knowledge(text: str, pathology: str, source: str) -> None:
    """
    Add a new reference document to the persistent knowledge base. This is
    what makes it a real, growable vector database rather than a fixed
    list -- call this to expand the corpus over time (e.g. with real
    curated clinical sources) without touching any code.
    """
    collection = _get_collection()
    # Content-derived id so repeat ingests upsert instead of colliding.
    # The old f"added_{count}" id was positional: a second run regenerated
    # the same ids and raised DuplicateIDError on the first document.
    digest = hashlib.sha1(f"{pathology}\x00{source}\x00{text}".encode("utf-8"))
    new_id = f"added_{digest.hexdigest()[:16]}"
    collection.upsert(
        documents=[text],
        metadatas=[{"pathology": pathology, "source": source}],
        ids=[new_id],
    )


def retrieve_evidence(pathology: str, top_k: int = 1) -> list:
    """
    Retrieve the most relevant reference passage(s) for a pathology finding
    via semantic similarity search against the persistent vector store.
    Same return shape as the previous in-memory version.
    """
    collection = _get_collection()
    results = collection.query(
        query_texts=[f"chest X-ray finding: {pathology}"],
        n_results=top_k,
    )

    output = []
    for doc, metadata, distance in zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    ):
        # Chroma returns cosine DISTANCE (0 = identical); convert to a
        # similarity score in the same 0-1-higher-is-better sense as before.
        similarity = 1 - distance
        output.append(
            {
                # queried_for/matched_pathology keep this a drop-in
                # replacement for evidence_agent.py: xray_api's /critique
                # and /report index match["queried_for"], and /evidence
                # reads match["matched_pathology"].
                "queried_for": pathology,
                "matched_pathology": metadata["pathology"],
                "pathology": metadata["pathology"],
                "text": doc,
                "source": metadata["source"],
                "similarity": similarity,
            }
        )
    return output


if __name__ == "__main__":
    for finding in ["Lung Opacity", "Pneumonia", "Effusion", "Fracture"]:
        print(f"\nQuery: {finding}")
        for r in retrieve_evidence(finding, top_k=1):
            print(f"  -> matched '{r['pathology']}' (similarity {r['similarity']:.2f})")
            print(f"     {r['text'][:100]}...")

    print(f"\nTotal documents in knowledge base: {_get_collection().count()}")
