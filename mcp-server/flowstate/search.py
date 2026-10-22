"""
FlowState Search Module - Unified semantic + FTS search.

This module provides:
1. Hybrid search combining vector similarity with full-text search
2. Weighted ranking of results
3. Content retrieval and snippet generation
"""

import sqlite3
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from . import dbconn
from .embeddings import get_embedding_service, EmbeddingService


@dataclass
class SearchResult:
    """A search result with metadata."""
    content_type: str
    content_id: int
    project_id: int
    title: str
    snippet: str
    score: float
    source: str  # 'semantic', 'fts', or 'hybrid'


class SearchService:
    """
    Unified search service combining semantic and full-text search.
    """
    
    def __init__(self, db_path: str):
        """
        Initialize the search service.
        
        Args:
            db_path: Path to SQLite database
        """
        self.db_path = db_path
        self.embedding_service = get_embedding_service(db_path)
    
    def _conn(self) -> sqlite3.Connection:
        """Get a database connection."""
        conn = dbconn.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def search(
        self,
        query: str,
        project_id: Optional[int] = None,
        content_types: Optional[list[str]] = None,
        limit: int = 10,
        semantic_weight: float = 0.6,
        fts_weight: float = 0.4
    ) -> list[SearchResult]:
        """
        Perform hybrid search combining semantic and FTS results.
        
        Args:
            query: Search query
            project_id: Optional project filter
            content_types: Optional list of content types
            limit: Maximum results
            semantic_weight: Weight for semantic search results (0-1)
            fts_weight: Weight for FTS results (0-1)
            
        Returns:
            List of SearchResult objects sorted by score
        """
        results = {}
        
        # 1. Semantic search (if available)
        if self.embedding_service.is_available:
            semantic_results = self.embedding_service.search_similar(
                query, project_id, content_types, limit * 2
            )
            for r in semantic_results:
                key = (r["content_type"], r["content_id"])
                results[key] = {
                    "content_type": r["content_type"],
                    "content_id": r["content_id"],
                    "project_id": r["project_id"],
                    "semantic_score": r["similarity"] * semantic_weight,
                    "fts_score": 0,
                    "source": "semantic"
                }
        
        # 2. Full-text search
        fts_results = self._fts_search(query, project_id, content_types, limit * 2)
        for r in fts_results:
            key = (r["content_type"], r["content_id"])
            if key in results:
                # Merge scores
                results[key]["fts_score"] = r["score"] * fts_weight
                results[key]["source"] = "hybrid"
            else:
                results[key] = {
                    "content_type": r["content_type"],
                    "content_id": r["content_id"],
                    "project_id": r["project_id"],
                    "semantic_score": 0,
                    "fts_score": r["score"] * fts_weight,
                    "source": "fts"
                }
        
        # 3. Calculate final scores and sort
        scored_results = []
        for key, data in results.items():
            final_score = data["semantic_score"] + data["fts_score"]
            scored_results.append({
                **data,
                "final_score": final_score
            })
        
        scored_results.sort(key=lambda x: x["final_score"], reverse=True)
        scored_results = scored_results[:limit]
        
        # 4. Enrich with content
        enriched = []
        for r in scored_results:
            content = self._get_content(r["content_type"], r["content_id"])
            if content:
                enriched.append(SearchResult(
                    content_type=r["content_type"],
                    content_id=r["content_id"],
                    project_id=r["project_id"],
                    title=content.get("title", ""),
                    snippet=content.get("snippet", ""),
                    score=r["final_score"],
                    source=r["source"]
                ))
        
        return enriched
    
    def _fts_search(
        self,
        query: str,
        project_id: Optional[int] = None,
        content_types: Optional[list[str]] = None,
        limit: int = 20
    ) -> list[dict]:
        """
        Perform full-text search.
        
        Args:
            query: Search query
            project_id: Optional project filter
            content_types: Optional content type filter
            limit: Maximum results
            
        Returns:
            List of results with scores
        """
        conn = self._conn()
        try:
            # Build query
            sql = """
                SELECT content_type, content_id, project_id,
                       bm25(memory_fts) as score
                FROM memory_fts
                WHERE searchable_text MATCH ?
            """
            params = [query]
            
            if project_id is not None:
                sql += " AND project_id = ?"
                params.append(project_id)
                
            if content_types:
                placeholders = ",".join("?" * len(content_types))
                sql += f" AND content_type IN ({placeholders})"
                params.extend(content_types)
                
            sql += f" ORDER BY score LIMIT ?"
            params.append(limit)
            
            results = conn.execute(sql, params).fetchall()
            
            return [
                {
                    "content_type": r["content_type"],
                    "content_id": r["content_id"],
                    "project_id": r["project_id"],
                    "score": abs(r["score"])  # bm25 returns negative scores
                }
                for r in results
            ]
        except sqlite3.OperationalError:
            # FTS table might not exist
            return []
        finally:
            conn.close()
    
    def _get_content(self, content_type: str, content_id: int) -> Optional[dict]:
        """
        Get content details for a search result.
        
        Args:
            content_type: Type of content
            content_id: ID of the content
            
        Returns:
            Dict with title and snippet
        """
        conn = self._conn()
        try:
            if content_type == "problem":
                row = conn.execute(
                    "SELECT title, description FROM problems WHERE id = ?",
                    (content_id,)
                ).fetchone()
                if row:
                    return {
                        "title": row["title"],
                        "snippet": (row["description"] or "")[:200]
                    }
                    
            elif content_type == "solution":
                row = conn.execute(
                    "SELECT summary, key_insight FROM solutions WHERE id = ?",
                    (content_id,)
                ).fetchone()
                if row:
                    return {
                        "title": row["summary"],
                        "snippet": (row["key_insight"] or "")[:200]
                    }
                    
            elif content_type == "learning":
                row = conn.execute(
                    "SELECT insight, context FROM learnings WHERE id = ?",
                    (content_id,)
                ).fetchone()
                if row:
                    return {
                        "title": row["insight"][:100],
                        "snippet": (row["context"] or row["insight"])[:200]
                    }
                    
            elif content_type == "change":
                row = conn.execute(
                    "SELECT field_name, old_value, new_value, reason FROM changes WHERE id = ?",
                    (content_id,)
                ).fetchone()
                if row:
                    return {
                        "title": f"{row['field_name']}: {row['old_value']} → {row['new_value']}",
                        "snippet": (row["reason"] or "")[:200]
                    }
            
            return None
        finally:
            conn.close()
    
    def index_content(
        self,
        content_type: str,
        content_id: int,
        project_id: int,
        text: str
    ) -> bool:
        """
        Index content for both semantic and FTS search.
        
        Args:
            content_type: Type of content
            content_id: ID of the content
            project_id: Project ID
            text: Text to index
            
        Returns:
            True if successful
        """
        success = True
        
        # Index for semantic search
        if self.embedding_service.is_available:
            success = self.embedding_service.store_embedding(
                content_type, content_id, project_id, text
            ) and success
        
        # Index for FTS
        conn = self._conn()
        try:
            # Delete existing entry
            conn.execute(
                "DELETE FROM memory_fts WHERE content_type = ? AND content_id = ?",
                (content_type, content_id)
            )
            
            # Insert new entry
            conn.execute(
                """INSERT INTO memory_fts (content_type, content_id, project_id, searchable_text)
                   VALUES (?, ?, ?, ?)""",
                (content_type, content_id, project_id, text)
            )
            conn.commit()
        except sqlite3.OperationalError as e:
            # FTS table might not exist
            print(f"Warning: FTS indexing failed: {e}")
            success = False
        finally:
            conn.close()
        
        return success
    
    def remove_from_index(self, content_type: str, content_id: int) -> bool:
        """
        Remove content from search indexes.
        
        Args:
            content_type: Type of content
            content_id: ID of the content
            
        Returns:
            True if successful
        """
        success = True
        
        # Remove from semantic index
        if self.embedding_service.is_available:
            success = self.embedding_service.delete_embedding(
                content_type, content_id
            ) and success
        
        # Remove from FTS index
        conn = self._conn()
        try:
            conn.execute(
                "DELETE FROM memory_fts WHERE content_type = ? AND content_id = ?",
                (content_type, content_id)
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()
        
        return success


# Singleton instance
_search_service: Optional[SearchService] = None


def get_search_service(db_path: str) -> SearchService:
    """Get or create the search service singleton."""
    global _search_service
    if _search_service is None or _search_service.db_path != db_path:
        _search_service = SearchService(db_path)
    return _search_service
