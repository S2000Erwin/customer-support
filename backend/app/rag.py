import hashlib
import logging
import re
from pathlib import Path

from google import genai
from qdrant_client import QdrantClient, models

from app.config import get_settings

logger = logging.getLogger("support_agent.rag")


def load_manual_chunks() -> list[dict[str, str]]:
    """Chunk the Markdown manual by heading topic, then by paragraph."""
    manual_path = Path(__file__).resolve().parents[1] / "manual" / "support-manual.md"
    content = manual_path.read_text(encoding="utf-8")
    chunks: list[dict[str, str]] = []
    heading_stack: list[tuple[int, str]] = []
    paragraph_lines: list[str] = []
    seen_ids: set[str] = set()

    def flush_paragraph() -> None:
        paragraph = "\n".join(line.strip() for line in paragraph_lines).strip()
        paragraph_lines.clear()
        if not paragraph:
            return

        topic = " > ".join(title for _, title in heading_stack) or "General"
        topic_slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:72]
        digest = hashlib.sha256(f"{topic}\0{paragraph}".encode()).hexdigest()[:12]
        chunk_id = f"{topic_slug}-{digest}"
        duplicate_number = 2
        while chunk_id in seen_ids:
            chunk_id = f"{topic_slug}-{digest}-{duplicate_number}"
            duplicate_number += 1
        seen_ids.add(chunk_id)
        chunks.append(
            {
                "id": chunk_id,
                "title": topic,
                "text": f"{topic}\n\n{paragraph}",
            }
        )

    for line in content.splitlines():
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
        elif line.strip():
            paragraph_lines.append(line)
        else:
            flush_paragraph()
    flush_paragraph()
    return chunks


class ManualRag:
    """Qdrant semantic search with a local keyword fallback for offline demos."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.manual_chunks = load_manual_chunks()
        self.client = None
        self.qdrant: QdrantClient | None = None
        self.ready = False

    async def initialize(self) -> None:
        if not self.settings.gemini_api_key:
            logger.info("rag_skipped reason=no_gemini_key fallback=keyword-demo")
            return
        try:
            self.client = genai.Client(api_key=self.settings.gemini_api_key)
            self.qdrant = QdrantClient(
                url=self.settings.qdrant_url,
                api_key=self.settings.qdrant_api_key or None,
                timeout=5,
            )
            if not self.qdrant.collection_exists(self.settings.qdrant_collection):
                try:
                    self.qdrant.create_collection(
                        collection_name=self.settings.qdrant_collection,
                        vectors_config=models.VectorParams(
                            size=768, distance=models.Distance.COSINE
                        ),
                    )
                except Exception:
                    # Serverless instances can start together; if a concurrent
                    # instance created the collection first (409), use it.
                    if not self.qdrant.collection_exists(
                        self.settings.qdrant_collection
                    ):
                        raise
            await self._sync_manual_points()
            self.ready = True
            logger.info(
                "qdrant_ready collection=%s points=%s",
                self.settings.qdrant_collection,
                self.qdrant.get_collection(
                    self.settings.qdrant_collection
                ).points_count,
            )
        except Exception as exc:  # noqa: BLE001
            self.ready = False
            logger.warning(
                "qdrant_unavailable fallback=keyword-demo error_type=%s error=%s",
                type(exc).__name__,
                str(exc).splitlines()[0][:300] if str(exc) else "",
            )

    async def _sync_manual_points(self) -> None:
        existing, _ = self.qdrant.scroll(
            collection_name=self.settings.qdrant_collection,
            limit=1000,
            with_payload=True,
            with_vectors=False,
        )
        existing_by_id = {
            point.payload.get("id"): point
            for point in existing
            if point.payload and point.payload.get("id")
        }
        numeric_point_ids = [
            point.id for point in existing if isinstance(point.id, int)
        ]
        next_point_id = max(numeric_point_ids, default=0) + 1
        changed: list[tuple[int, dict[str, str]]] = []
        for item in self.manual_chunks:
            existing_point = existing_by_id.get(item["id"])
            if existing_point is None:
                changed.append((next_point_id, item))
                next_point_id += 1
            elif any(
                existing_point.payload.get(key) != item[key]
                for key in ("title", "text")
            ):
                changed.append((existing_point.id, item))
        if changed:
            vectors = await self._embed(
                [f"{item['title']}. {item['text']}" for _, item in changed]
            )
            self.qdrant.upsert(
                collection_name=self.settings.qdrant_collection,
                points=[
                    models.PointStruct(id=point_id, vector=vector, payload=item)
                    for (point_id, item), vector in zip(changed, vectors)
                ],
            )
        valid_ids = {item["id"] for item in self.manual_chunks}
        stale_point_ids = [
            point.id
            for point in existing
            if point.payload
            and point.payload.get("id")
            and point.payload["id"] not in valid_ids
        ]
        if stale_point_ids:
            self.qdrant.delete(
                collection_name=self.settings.qdrant_collection,
                points_selector=models.PointIdsList(points=stale_point_ids),
            )
        logger.info(
            "manual_index_synced chunks=%s updated=%s removed=%s",
            len(self.manual_chunks),
            len(changed),
            len(stale_point_ids),
        )

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        result = await self.client.aio.models.embed_content(
            model="gemini-embedding-001",
            contents=texts,
            config={"output_dimensionality": 768},
        )
        return [embedding.values for embedding in result.embeddings]

    async def search(self, query: str) -> dict:
        if self.ready and self.qdrant is not None:
            try:
                retrieval_query = query
                # Small model call is isolated to retrieval preparation; the main agent owns the answer.
                try:
                    rewrite = await self.client.aio.models.generate_content(
                        model=self.settings.gemini_rag_model,
                        contents=f"Rewrite this support question as a concise search query. Return only the query.\nQuestion: {query}",
                        config={"temperature": 0.1, "max_output_tokens": 48},
                    )
                    retrieval_query = (rewrite.text or query).strip()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "rag_query_rewrite_failed error_type=%s fallback=original-query",
                        type(exc).__name__,
                    )
                vector = (await self._embed([retrieval_query]))[0]
                result = self.qdrant.query_points(
                    collection_name=self.settings.qdrant_collection,
                    query=vector,
                    limit=3,
                    with_payload=True,
                )
                scored_points = [
                    (point, float(point.score or 0.0)) for point in result.points
                ]
                confidence = max((score for _, score in scored_points), default=0.0)
                matches = [
                    {**dict(point.payload or {}), "similarity": score}
                    for point, score in scored_points
                    if score >= self.settings.rag_min_similarity
                ]
                low_confidence = not matches
                logger.info(
                    "rag_search backend=qdrant hits=%s confidence=%.3f threshold=%.3f guardrail=%s",
                    len(matches),
                    confidence,
                    self.settings.rag_min_similarity,
                    "escalate" if low_confidence else "pass",
                )
                return {
                    "ok": True,
                    "matches": matches,
                    "sources": [
                        {
                            "id": item.get("id", ""),
                            "title": item.get("title", "Support manual"),
                        }
                        for item in matches
                    ],
                    "retrieval": "qdrant",
                    "confidence_score": confidence,
                    "confidence_threshold": self.settings.rag_min_similarity,
                    "confidence_details": {
                        "formula": "maximum cosine similarity across the top 3 manual chunks",
                        "query": retrieval_query,
                        "top_scores": [round(score, 4) for _, score in scored_points],
                    },
                    "low_confidence": low_confidence,
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "rag_search_failed error_type=%s fallback=keyword-demo",
                    type(exc).__name__,
                )
        stop_words = {
            "about",
            "after",
            "and",
            "are",
            "can",
            "could",
            "does",
            "for",
            "from",
            "have",
            "help",
            "how",
            "i",
            "is",
            "it",
            "me",
            "my",
            "of",
            "please",
            "tell",
            "the",
            "this",
            "to",
            "what",
            "when",
            "where",
            "which",
            "who",
            "would",
            "you",
            "your",
        }

        def normalize_terms(text: str) -> set[str]:
            terms = {
                word
                for word in re.findall(r"[a-z0-9]+", text.lower())
                if len(word) > 2 and word not in stop_words
            }
            return {
                word[:-1] if word.endswith("s") and len(word) > 4 else word
                for word in terms
            }

        terms = normalize_terms(query)
        ranked = []
        for item in self.manual_chunks:
            chunk_terms = normalize_terms(f"{item['title']} {item['text']}")
            overlap = len(terms & chunk_terms) / len(terms) if terms else 0.0
            ranked.append((overlap, item))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        confidence = ranked[0][0] if ranked else 0.0
        best_terms = (
            normalize_terms(f"{ranked[0][1]['title']} {ranked[0][1]['text']}")
            if ranked
            else set()
        )
        matched_terms = sorted(terms & best_terms)
        matches = [
            item
            for score, item in ranked[:2]
            if score >= self.settings.rag_min_keyword_overlap
        ]
        low_confidence = not matches
        logger.info(
            "rag_search backend=keyword-demo hits=%s confidence=%.3f threshold=%.3f guardrail=%s",
            len(matches),
            confidence,
            self.settings.rag_min_keyword_overlap,
            "escalate" if low_confidence else "pass",
        )
        return {
            "ok": True,
            "matches": matches,
            "sources": [{"id": item["id"], "title": item["title"]} for item in matches],
            "retrieval": "keyword-demo",
            "confidence_score": confidence,
            "confidence_threshold": self.settings.rag_min_keyword_overlap,
            "confidence_details": {
                "formula": "matched distinct query terms / distinct non-stopword query terms",
                "query_terms": sorted(terms),
                "matched_terms": matched_terms,
                "matched_count": len(matched_terms),
                "query_term_count": len(terms),
            },
            "low_confidence": low_confidence,
        }


manual_rag = ManualRag()
