"""
Ingests text files from ./knowledge_base/ into the persistent ChromaDB
vector store used by evidence_agent_chroma.py.

Folder structure expected:
    knowledge_base/
        manifest.json      <- maps each file to its pathology + source
        pneumonia.txt       <- the actual reference text content
        effusion.txt
        ... one .txt file per reference document

manifest.json format:
    [
      {"file": "pneumonia.txt", "pathology": "Pneumonia", "source": "https://..."},
      ...
    ]

Run this any time you add a new file + manifest entry:
    python ingest_knowledge.py

Safe to re-run -- it just adds new documents; it doesn't delete or
duplicate-check existing ones, so don't re-run with the same file twice
without removing the old entry first (see note at the bottom).
"""

import json
import os

from evidence_agent_chroma import add_knowledge, _get_collection

KNOWLEDGE_DIR = "knowledge_base"
MANIFEST_PATH = os.path.join(KNOWLEDGE_DIR, "manifest.json")


def ingest():
    if not os.path.exists(MANIFEST_PATH):
        print(f"No manifest found at {MANIFEST_PATH}. Create it first.")
        return

    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    for entry in manifest:
        file_path = os.path.join(KNOWLEDGE_DIR, entry["file"])
        if not os.path.exists(file_path):
            print(f"  SKIPPED: {entry['file']} not found in {KNOWLEDGE_DIR}/")
            continue

        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read().strip()

        add_knowledge(text=text, pathology=entry["pathology"], source=entry["source"])
        print(f"  Added: {entry['file']} -> pathology='{entry['pathology']}'")

    total = _get_collection().count()
    print(f"\nDone. Knowledge base now has {total} total documents.")


if __name__ == "__main__":
    ingest()
