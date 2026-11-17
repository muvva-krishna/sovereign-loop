from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from contracts.config import PROJECT_ROOT, get_settings

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "with",
    }
)


class SopSearchHit(BaseModel):
    """Ranked SOP section match returned by hybrid BM25/TF-IDF search."""

    doc_id: str
    title: str
    section: str
    snippet: str
    score: float
    tenant_scope: str


class SopDocument(BaseModel):
    """Full SOP document metadata and markdown content."""

    doc_id: str
    title: str
    tenant_scope: str
    content: str


@dataclass(frozen=True)
class _SopChunk:
    doc_id: str
    tenant_scope: str
    title: str
    section: str
    content: str
    tokens: tuple[str, ...]
    header_tokens: frozenset[str]


def _tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in words if len(w) >= 2 and w not in _STOPWORDS]


def _resolve_sops_dir(sops_dir: str | Path | None = None) -> Path:
    if sops_dir is not None:
        path = Path(sops_dir)
    else:
        raw = os.environ.get("SOPS_DIR") or get_settings().sops_dir
        path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _parse_markdown_sop(path: Path) -> tuple[dict[str, str], str]:
    raw_text = path.read_text(encoding="utf-8")
    metadata: dict[str, str] = {}
    body = raw_text

    frontmatter_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", raw_text, re.DOTALL)
    if frontmatter_match:
        fm_raw, body = frontmatter_match.group(1), frontmatter_match.group(2)
        parsed: Any = yaml.safe_load(fm_raw)
        if isinstance(parsed, dict):
            metadata = {str(k).strip(): str(v).strip() for k, v in parsed.items() if v is not None}
    else:
        lines = raw_text.splitlines()
        body_start = 0
        for idx, line in enumerate(lines[:10]):
            if ":" in line and not line.startswith("#"):
                key, val = line.split(":", 1)
                metadata[key.strip().lower()] = val.strip()
                body_start = idx + 1
            elif line.strip() == "":
                body_start = idx + 1
                break
            else:
                break
        if metadata:
            body = "\n".join(lines[body_start:])

    doc_id = metadata.get("doc_id") or path.stem.split("_", 1)[0]
    tenant_scope = metadata.get("tenant_scope") or "all"
    title = metadata.get("title") or path.stem.replace("_", " ").title()
    return {"doc_id": doc_id, "tenant_scope": tenant_scope, "title": title}, body.strip()


def _split_sections(title: str, body: str) -> list[tuple[str, str]]:
    """Split markdown document into (section_heading, section_body) tuples."""
    sections: list[tuple[str, str]] = []
    current_heading = title
    current_lines: list[str] = []

    for line in body.splitlines():
        heading_match = re.match(r"^(#{2,3})\s+(.+)$", line.strip())
        if heading_match:
            if current_lines:
                text = "\n".join(current_lines).strip()
                if text:
                    sections.append((current_heading, text))
            current_heading = heading_match.group(2).strip()
            current_lines = []
        else:
            # Skip redundant top-level H1 if identical to title
            if line.strip().startswith("# ") and not current_lines and not sections:
                continue
            current_lines.append(line)

    if current_lines:
        text = "\n".join(current_lines).strip()
        if text:
            sections.append((current_heading, text))

    if not sections and body.strip():
        sections.append((title, body.strip()))
    return sections


def _is_tenant_allowed(tenant_scope: str, tenant_id: str) -> bool:
    scopes = [s.strip() for s in tenant_scope.split(",") if s.strip()]
    return "all" in scopes or tenant_id in scopes


def _extract_snippet(content: str, query_tokens: list[str], max_chars: int = 450) -> str:
    compact = re.sub(r"\s+", " ", content).strip()
    if len(compact) <= max_chars:
        return compact

    lower = compact.lower()
    best_pos = 0
    for token in query_tokens:
        pos = lower.find(token)
        if pos != -1:
            best_pos = max(0, pos - 80)
            break

    snippet = compact[best_pos : best_pos + max_chars]
    if best_pos > 0:
        snippet = "..." + snippet
    if best_pos + max_chars < len(compact):
        snippet = snippet + "..."
    return snippet


class SopSearchIndex:
    """In-memory BM25 / TF-IDF search index over tenant-scoped SOP markdown files."""

    def __init__(self, sops_dir: str | Path | None = None) -> None:
        self.sops_dir = _resolve_sops_dir(sops_dir)
        self._documents: dict[str, SopDocument] = {}
        self._chunks: list[_SopChunk] = []
        self._doc_freqs: Counter[str] = Counter()
        self._avg_chunk_len: float = 1.0
        self._load()

    def _load(self) -> None:
        if not self.sops_dir.exists():
            return

        for md_path in sorted(self.sops_dir.glob("*.md")):
            meta, body = _parse_markdown_sop(md_path)
            doc = SopDocument(
                doc_id=meta["doc_id"],
                title=meta["title"],
                tenant_scope=meta["tenant_scope"],
                content=body,
            )
            self._documents[doc.doc_id] = doc

            for section_name, section_body in _split_sections(doc.title, body):
                tokens = tuple(_tokenize(f"{doc.title} {section_name} {section_body}"))
                header_tokens = frozenset(_tokenize(f"{doc.title} {section_name}"))
                self._chunks.append(
                    _SopChunk(
                        doc_id=doc.doc_id,
                        tenant_scope=doc.tenant_scope,
                        title=doc.title,
                        section=section_name,
                        content=section_body,
                        tokens=tokens,
                        header_tokens=header_tokens,
                    )
                )

        if self._chunks:
            for chunk in self._chunks:
                for term in set(chunk.tokens):
                    self._doc_freqs[term] += 1
            self._avg_chunk_len = sum(len(c.tokens) for c in self._chunks) / len(self._chunks) or 1.0

    def search(self, tenant_id: str, query: str, top_k: int = 3) -> list[SopSearchHit]:
        """Search SOP chunks strictly filtered by tenant_id using BM25 + header boost."""
        tid = tenant_id.strip() if tenant_id else ""
        if not tid:
            raise ValueError("tenant_id is required and cannot be empty.")

        query_tokens = _tokenize(query)
        if not query_tokens or not self._chunks:
            return []

        clamped_k = max(1, min(top_k, 20))
        n_docs = len(self._chunks)
        k1 = 1.5
        b = 0.75
        norm_query = " ".join(query_tokens)

        scored: list[SopSearchHit] = []
        for chunk in self._chunks:
            if not _is_tenant_allowed(chunk.tenant_scope, tid):
                continue
            if not chunk.tokens:
                continue

            tf_counts = Counter(chunk.tokens)
            doc_len = len(chunk.tokens)
            bm25_score = 0.0

            for term in query_tokens:
                tf = tf_counts.get(term, 0)
                if tf == 0:
                    continue
                df = self._doc_freqs.get(term, 0)
                idf = math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)
                denom = tf + k1 * (1.0 - b + b * (doc_len / self._avg_chunk_len))
                bm25_score += idf * ((tf * (k1 + 1.0)) / denom)

            if bm25_score <= 0.0:
                continue

            # Boost when query terms appear in title/section or exact phrase matches
            header_hits = sum(1 for term in set(query_tokens) if term in chunk.header_tokens)
            header_boost = 1.0 + 0.25 * header_hits
            phrase_boost = 1.2 if len(query_tokens) >= 2 and norm_query in chunk.content.lower() else 1.0
            final_score = round(bm25_score * header_boost * phrase_boost, 4)

            scored.append(
                SopSearchHit(
                    doc_id=chunk.doc_id,
                    title=chunk.title,
                    section=chunk.section,
                    snippet=_extract_snippet(chunk.content, query_tokens),
                    score=final_score,
                    tenant_scope=chunk.tenant_scope,
                )
            )

        scored.sort(key=lambda hit: (-hit.score, hit.doc_id, hit.section))
        return scored[:clamped_k]

    def get_document(self, tenant_id: str, doc_id: str) -> SopDocument:
        """Return full SOP document if it exists and is accessible to tenant_id."""
        tid = tenant_id.strip() if tenant_id else ""
        if not tid:
            raise ValueError("tenant_id is required and cannot be empty.")

        doc = self._documents.get(doc_id)
        if not doc or not _is_tenant_allowed(doc.tenant_scope, tid):
            raise ValueError(f"SOP document '{doc_id}' not found for tenant '{tid}'.")
        return doc


_INDEX_CACHE: dict[Path, SopSearchIndex] = {}


def get_search_index(sops_dir: str | Path | None = None) -> SopSearchIndex:
    """Return cached SopSearchIndex for the resolved SOP directory."""
    resolved = _resolve_sops_dir(sops_dir)
    if resolved not in _INDEX_CACHE:
        _INDEX_CACHE[resolved] = SopSearchIndex(resolved)
    return _INDEX_CACHE[resolved]


def search_sops(
    tenant_id: str,
    query: str,
    top_k: int = 3,
    sops_dir: str | Path | None = None,
) -> list[SopSearchHit]:
    """Search tenant-visible SOPs and return top_k ranked chunks."""
    return get_search_index(sops_dir).search(tenant_id=tenant_id, query=query, top_k=top_k)


def get_sop_document(
    tenant_id: str,
    doc_id: str,
    sops_dir: str | Path | None = None,
) -> SopDocument:
    """Retrieve full SOP document enforcing tenant_scope isolation."""
    return get_search_index(sops_dir).get_document(tenant_id=tenant_id, doc_id=doc_id)
