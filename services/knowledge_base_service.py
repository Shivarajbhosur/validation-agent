"""
Knowledge Base Service - ChromaDB wrapper for storing and searching PR fixes.
"""

import chromadb
from chromadb.config import Settings
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path

import config
from utils.logger import get_logger

logger = get_logger("knowledge_base_service")


class KnowledgeBaseService:
    """Service for managing the Knowledge Base using ChromaDB."""
    
    def __init__(self):
        """Initialize ChromaDB client."""
        self.db_path = Path(config.BASE_DIR) / "data" / "knowledge_db"
        self.db_path.mkdir(parents=True, exist_ok=True)
        
        # Initialize ChromaDB client (persistent storage)
        self.client = chromadb.PersistentClient(path=str(self.db_path))
        
        # Get or create collection for PR fixes
        self.collection = self.client.get_or_create_collection(
            name="pr_fixes",
            metadata={"description": "Past PR fixes for validation errors"}
        )
        
        logger.info(f"KB initialized at: {self.db_path}")
        logger.info(f"Collection 'pr_fixes' has {self.collection.count()} entries")
    
    def add_pr_fix(
        self,
        pr_id: int,
        baseline: str,
        ticket_id: int,
        title: str,
        description: str,
        files_changed: List[str],
        code_diff: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Add a PR fix to the Knowledge Base.
        
        Args:
            pr_id: PR number
            baseline: Baseline name (e.g., P02070-HPPL486P)
            ticket_id: Linked ticket number
            title: PR title
            description: PR description
            files_changed: List of changed files
            code_diff: Combined code diff
            metadata: Additional metadata
        
        Returns:
            bool: Success
        """
        try:
            # Create document text for embedding
            doc_text = f"""
Baseline: {baseline}
Title: {title}
Description: {description}
Files Changed: {', '.join(files_changed)}
Code Changes:
{code_diff}
"""
            
            # Prepare metadata
            meta = {
                "pr_id": pr_id,
                "baseline": baseline,
                "ticket_id": ticket_id,
                "title": title,
                "files_changed": ",".join(files_changed),
                "source": "azure_devops_pr"
            }
            if metadata:
                meta.update(metadata)
            
            # Add to collection
            self.collection.add(
                ids=[f"pr-{pr_id}"],
                documents=[doc_text],
                metadatas=[meta]
            )
            
            logger.info(f"Added PR {pr_id} to KB: {baseline}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to add PR {pr_id} to KB: {e}")
            return False
    
    
    def pr_exists(self, pr_id: int) -> bool:
        """Check if a PR already exists in KB."""
        try:
            result = self.collection.get(ids=[f"pr-{pr_id}"])
            return len(result['ids']) > 0
        except Exception:
            return False
        
        
    def search_similar(
        self,
        query: str,
        n_results: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Search for similar errors/fixes in KB.
        
        Args:
            query: Error message or description
            n_results: Number of results to return
        
        Returns:
            List of similar PR fixes
        """
        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=n_results
            )
            
            # Format results
            fixes = []
            if results and results['ids'] and results['ids'][0]:
                for i, doc_id in enumerate(results['ids'][0]):
                    fix = {
                        "id": doc_id,
                        "document": results['documents'][0][i] if results['documents'] else "",
                        "metadata": results['metadatas'][0][i] if results['metadatas'] else {},
                        "distance": results['distances'][0][i] if results['distances'] else 0
                    }
                    fixes.append(fix)
            
            logger.info(f"Found {len(fixes)} similar fixes for query")
            return fixes
            
        except Exception as e:
            logger.error(f"KB search failed: {e}")
            return []
    
    def search_keyword(self, query: str, n_results: int = 10) -> List[Dict[str, Any]]:
        """
        Keyword search - finds text matches in PRs using individual terms.
        
        Args:
            query: Text to search for (will split into terms)
            n_results: Maximum number of results
        
        Returns:
            List of matching PRs
        """
        try:
            # Get all documents from collection
            all_docs = self.collection.get(
                include=["documents", "metadatas"]
            )
            
            matches = []
            
            # Split query into individual terms (words)
            query_terms = [term.lower() for term in query.split() if len(term) >= 3]
            
            if all_docs and all_docs['documents']:
                for i, doc in enumerate(all_docs['documents']):
                    if not doc:
                        continue
                        
                    doc_lower = doc.lower()
                    metadata = all_docs['metadatas'][i] if all_docs['metadatas'] else {}
                    
                    # Get title/baseline for boosting
                    title = metadata.get('title', '').lower()
                    baseline = metadata.get('baseline', '').lower()
                    
                    # Count how many query terms are found
                    terms_found = 0
                    total_matches = 0
                    title_boost = 0
                    
                    for term in query_terms:
                        if term in doc_lower:
                            terms_found += 1
                            total_matches += doc_lower.count(term)
                        
                        # Boost if term is in title or baseline (program name match!)
                        if term in title or term in baseline:
                            title_boost += 500  # Big boost for title/baseline match!
                    
                    # Only include if at least one term found
                    if terms_found > 0:
                        matches.append({
                            "id": all_docs['ids'][i],
                            "document": doc,
                            "metadata": metadata,
                            "match_count": total_matches + title_boost,  # Include boost in match_count
                            "terms_found": terms_found,
                            "title_boost": title_boost,
                            "search_type": "keyword"
                        })
            
            # Sort by terms_found first, then by match_count
            matches.sort(key=lambda x: (x['terms_found'], x['match_count']), reverse=True)
            
            return matches[:n_results]
            
        except Exception as e:
            logger.error(f"Keyword search failed: {e}")
            return []

    def search_hybrid(self, query: str, n_results: int = 10) -> List[Dict[str, Any]]:
        """
        Hybrid search - combines keyword (exact) and semantic (meaning) search.
        
        Args:
            query: Search query
            n_results: Maximum number of results
        
        Returns:
            Combined and ranked results
        """
        try:
            # Get keyword results (exact match)
            keyword_results = self.search_keyword(query, n_results=n_results)
            
            # Get semantic results (meaning match)
            semantic_results = self.search_similar(query, n_results=n_results)
            
            # Combine results
            combined = {}
            
            # Add keyword results (higher priority)
            for r in keyword_results:
                pr_id = r['id']
                terms_found = r.get('terms_found', 0)
                match_count = r.get('match_count', 0)
                
                # Score: prioritize terms_found, then match_count
                score = (terms_found * 100) + (match_count * 10)
                
                combined[pr_id] = {
                    "id": pr_id,
                    "metadata": r['metadata'],
                    "keyword_match": True,
                    "match_count": match_count,
                    "terms_found": terms_found,
                    "semantic_distance": None,
                    "score": score
                }
            
            # Add semantic results
            for r in semantic_results:
                pr_id = r['id']
                distance = r.get('distance', 2.0)
                semantic_score = max(0, (2.0 - distance) * 50)  # Convert distance to score
                
                if pr_id in combined:
                    # Already found by keyword - boost score
                    combined[pr_id]['semantic_distance'] = distance
                    combined[pr_id]['score'] += semantic_score
                else:
                    # Only found by semantic
                    combined[pr_id] = {
                        "id": pr_id,
                        "metadata": r['metadata'],
                        "keyword_match": False,
                        "match_count": 0,
                        "terms_found": 0,
                        "semantic_distance": distance,
                        "score": semantic_score
                    }
            
            # Sort by score (highest first)
            results = sorted(combined.values(), key=lambda x: x['score'], reverse=True)
            
            return results[:n_results]
            
        except Exception as e:
            logger.error(f"Hybrid search failed: {e}")
            return []
    
    # ── Issues collection (curated bug patterns + fixes) ──────────────
    # Separate from ``pr_fixes``: entries are human-curated known bug
    # patterns with their exact fix. Retrieved during analysis to surface
    # candidate "known issue" matches that the AI can adapt to the
    # current program (even when identifiers differ).

    def _get_issues_collection(self):
        """Lazy-init the ``issues`` collection."""
        if not hasattr(self, "_issues_collection") or self._issues_collection is None:
            self._issues_collection = self.client.get_or_create_collection(
                name="issues",
                metadata={"description": "Curated known bug patterns and fixes"},
            )
            logger.info(
                f"Collection 'issues' has {self._issues_collection.count()} entries"
            )
        return self._issues_collection

    def add_issue(
        self,
        issue_id: str,
        title: str,
        buggy_code: str,
        fixed_code: str,
        bug_category: str = "other",
        tags: Optional[List[str]] = None,
        program: str = "",
        error_signature: str = "",
        symptoms: str = "",
        root_cause: str = "",
        source: str = "manual-chat",
        resolved_date: str = "",
        resolved_by: str = "",
        verified: bool = False,
    ) -> bool:
        """Add a curated bug pattern to the ``issues`` collection.

        The document text used for embedding intentionally emphasizes
        the buggy code, error signature, and tags — NOT the fix — so
        retrieval matches on the BUG PATTERN, not the resolution.
        """
        try:
            collection = self._get_issues_collection()

            tags_list = tags or []
            doc_text = (
                f"Title: {title}\n"
                f"Category: {bug_category}\n"
                f"Tags: {', '.join(tags_list)}\n"
                f"Program: {program}\n"
                f"Error: {error_signature}\n"
                f"Symptoms: {symptoms}\n"
                f"Buggy code:\n{buggy_code}"
            )

            meta = {
                "issue_id": issue_id,
                "title": title,
                "program": program,
                "error_signature": error_signature,
                "symptoms": symptoms,
                "root_cause": root_cause,
                "buggy_code": buggy_code,
                "fixed_code": fixed_code,
                "bug_category": bug_category,
                "tags": ",".join(tags_list),
                "source": source,
                "resolved_date": resolved_date,
                "resolved_by": resolved_by,
                "verified": verified,
            }

            collection.add(
                ids=[issue_id],
                documents=[doc_text],
                metadatas=[meta],
            )
            logger.info(f"Added issue {issue_id} to KB: {title}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to add issue {issue_id}: {e}")
            return False

    def count_issues(self) -> int:
        """Total number of curated issues in the KB."""
        try:
            return self._get_issues_collection().count()
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to count issues: {e}")
            return 0

    def issue_exists(self, issue_id: str) -> bool:
        """Return True if an issue with this id is already stored."""
        try:
            result = self._get_issues_collection().get(ids=[issue_id])
            return len(result.get("ids", [])) > 0
        except Exception:  # noqa: BLE001
            return False

    def search_issues(
        self,
        query: str,
        n_results: int = 10,
        max_distance: float = 0.6,
    ) -> List[Dict[str, Any]]:
        """Return curated issues whose embedding is close to *query*.

        Only entries with ``distance <= max_distance`` are returned. When
        the collection is empty, an empty list is returned immediately —
        the caller gets zero overhead on a fresh install.
        """
        try:
            collection = self._get_issues_collection()
            if collection.count() == 0:
                return []

            results = collection.query(
                query_texts=[query],
                n_results=min(n_results, collection.count()),
            )

            hits: List[Dict[str, Any]] = []
            raw_distances: List[Tuple[str, float]] = []
            if results and results.get("ids") and results["ids"][0]:
                for i, doc_id in enumerate(results["ids"][0]):
                    distance = (
                        results["distances"][0][i]
                        if results.get("distances")
                        else 1.0
                    )
                    raw_distances.append((str(doc_id), float(distance)))
                    if distance > max_distance:
                        continue
                    hits.append(
                        {
                            "id": doc_id,
                            "document": results["documents"][0][i]
                            if results.get("documents")
                            else "",
                            "metadata": results["metadatas"][0][i]
                            if results.get("metadatas")
                            else {},
                            "distance": distance,
                        }
                    )
            if raw_distances:
                top_dbg = ", ".join(f"{i}={d:.3f}" for i, d in raw_distances[:3])
                logger.info(
                    f"Issues candidates (top 3 by distance): {top_dbg}"
                )
            logger.info(
                f"Found {len(hits)} issues (dist<={max_distance}) for query"
            )
            return hits
        except Exception as e:  # noqa: BLE001
            logger.error(f"Issues search failed: {e}")
            return []

    def list_issues(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return up to *limit* stored issues (metadata only)."""
        try:
            collection = self._get_issues_collection()
            result = collection.get(include=["metadatas"], limit=limit)
            entries = []
            for i, doc_id in enumerate(result.get("ids", [])):
                entries.append(
                    {
                        "id": doc_id,
                        "metadata": result["metadatas"][i]
                        if result.get("metadatas")
                        else {},
                    }
                )
            return entries
        except Exception as e:  # noqa: BLE001
            logger.error(f"list_issues failed: {e}")
            return []

    def delete_issue(self, issue_id: str) -> bool:
        """Delete a single curated issue by id."""
        try:
            self._get_issues_collection().delete(ids=[issue_id])
            logger.info(f"Deleted issue {issue_id}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to delete issue {issue_id}: {e}")
            return False

    def get_stats(self) -> Dict[str, Any]:
        """Get KB statistics."""
        return {
            "total_entries": self.collection.count(),
            "collection_name": "pr_fixes",
            "db_path": str(self.db_path)
        }
    
    def clear(self) -> bool:
        """Clear all entries (use with caution!)."""
        try:
            self.client.delete_collection("pr_fixes")
            self.collection = self.client.get_or_create_collection(
                name="pr_fixes",
                metadata={"description": "Past PR fixes for validation errors"}
            )
            logger.info("KB cleared")
            return True
        except Exception as e:
            logger.error(f"Failed to clear KB: {e}")
            return False


# Singleton instance
_kb_service: Optional[KnowledgeBaseService] = None


def get_kb_service() -> KnowledgeBaseService:
    """Get singleton KB service instance."""
    global _kb_service
    if _kb_service is None:
        _kb_service = KnowledgeBaseService()
    return _kb_service