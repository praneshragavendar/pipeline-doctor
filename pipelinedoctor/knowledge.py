"""Knowledge base retrieval - the "R" in RAG.

Finds past incidents most similar to the current failure using TF-IDF + cosine
similarity, written in plain Python so it runs anywhere without a vector database.
"""

import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from pipelinedoctor.models import Incident

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_KB = DATA_DIR / "past_incidents.yaml"

TOKEN = re.compile(r"[a-z0-9][a-z0-9+#.\-]*")
STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "is", "was",
    "with", "at", "by", "from", "it", "this", "that", "be", "not", "but", "as",
}


def tokenize(text: str) -> List[str]:
    """Lowercase words, dropping very common words."""
    return [t.strip(".-") for t in TOKEN.findall(text.lower()) if t not in STOPWORDS]


def load_incidents(path: Optional[str] = None) -> List[Incident]:
    """Load past incidents from YAML (CLI flag > env var > bundled default)."""
    chosen = path or os.environ.get("PIPELINEDOCTOR_KB")
    file_path = Path(chosen) if chosen else DEFAULT_KB
    if not file_path.is_file():
        return []
    data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    return [
        Incident(
            id=item["id"],
            title=item["title"],
            symptoms=" ".join(item.get("symptoms", "").split()),
            root_cause=item.get("root_cause", ""),
            fix=item.get("fix", ""),
            tags=item.get("tags", []),
        )
        for item in data.get("incidents", [])
    ]


class KnowledgeBase:
    """Small TF-IDF index over past incidents."""

    def __init__(self, incidents: List[Incident]):
        self.incidents = incidents
        self.docs = [self._incident_text(i) for i in incidents]
        self.doc_tokens = [tokenize(d) for d in self.docs]
        doc_count = len(self.docs) or 1
        df = Counter(t for tokens in self.doc_tokens for t in set(tokens))
        # Smoothed inverse document frequency: rare words matter more.
        self.idf = {t: math.log((1 + doc_count) / (1 + n)) + 1 for t, n in df.items()}
        self.doc_vectors = [self._vector(tokens) for tokens in self.doc_tokens]

    @staticmethod
    def _incident_text(incident: Incident) -> str:
        # Tags are repeated so they weigh more than free text.
        tags = " ".join(incident.tags) * 2
        return f"{incident.title} {incident.symptoms} {incident.root_cause} {tags}"

    def _vector(self, tokens: List[str]) -> Dict[str, float]:
        counts = Counter(tokens)
        total = sum(counts.values()) or 1
        return {t: (c / total) * self.idf.get(t, 0.0) for t, c in counts.items()}

    @staticmethod
    def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
        dot = sum(v * b.get(k, 0.0) for k, v in a.items())
        norm_a = math.sqrt(sum(v * v for v in a.values()))
        norm_b = math.sqrt(sum(v * v for v in b.values()))
        return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0

    def search(self, query: str, top_k: int = 2, min_score: float = 0.15) -> List[Tuple[Incident, float]]:
        """Return the top_k most similar incidents with their similarity score."""
        if not self.incidents:
            return []
        query_vector = self._vector(tokenize(query))
        scored = [
            (incident, self._cosine(query_vector, vector))
            for incident, vector in zip(self.incidents, self.doc_vectors)
        ]
        scored = [pair for pair in scored if pair[1] >= min_score]
        return sorted(scored, key=lambda pair: pair[1], reverse=True)[:top_k]
