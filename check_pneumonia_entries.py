"""
One-off check: see ALL matching entries for "Pneumonia" in the knowledge
base, not just the top 1 -- confirms both the seed entry and your new
fpnotebook-sourced entry actually exist as distinct documents.
"""

from evidence_agent_chroma import retrieve_evidence

results = retrieve_evidence("Pneumonia", top_k=2)

for i, r in enumerate(results, 1):
    print(f"\n--- Result {i} (similarity {r['similarity']:.2f}) ---")
    print(f"Source: {r['source']}")
    print(f"Text: {r['text']}")
