"""
FlowState Embeddings Module - Semantic search with sqlite-vec.

This module provides:
1. Text embedding generation using sentence-transformers
2. Vector storage and retrieval using sqlite-vec
3. Hybrid search combining semantic similarity with FTS
"""

import sqlite3
from pathlib import Path
from typing import Optional
import json

from . import dbconn

# Optional imports - gracefully degrade if not available
try:
    from sentence_transformers import SentenceTransformer
    EMBEDDINGS_AVAILABLE = True
except ImportError:
    EMBEDDINGS_AVAILABLE = False
    SentenceTransformer = None

try:
    import sqlite_vec
    SQLITE_VEC_AVAILABLE = True
except ImportError:
    SQLITE_VEC_AVAILABLE = False
    sqlite_vec = None


# Default model - small but effective
DEFAULT_MODEL = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384  # Dimension for all-MiniLM-L6-v2


class EmbeddingService:
    """
    Handles embedding generation and vector search.
    
    Falls back to FTS-only search if dependencies aren't available.
    """
    
    def __init__(self, db_path: str, model_name: str = DEFAULT_MODEL):
        """
        Initialize the embedding service.
        
        Args:
            db_path: Path to SQLite database
            model_name: Sentence transformer model to use
        """
        self.db_path = db_path
        self.model_name = model_name
        self._model = None
        self._initialized = False
        
    @property
    def is_available(self) -> bool:
        """Check if semantic search is available."""
        return EMBEDDINGS_AVAILABLE and SQLITE_VEC_AVAILABLE
    
    @property
    def model(self) -> Optional["SentenceTransformer"]:
        """Lazy-load the embedding model."""
        if self._model is None and EMBEDDINGS_AVAILABLE:
            self._model = SentenceTransformer(self.model_name)
        return self._model
    
    def _conn(self) -> sqlite3.Connection:
        """Get a database connection with sqlite-vec loaded."""
        conn = dbconn.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        
        # Load sqlite-vec extension if available
        if SQLITE_VEC_AVAILABLE:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            
        return conn
    
    def initialize(self) -> bool:
        """
        Initialize the embeddings table if needed.
        
        Returns:
            True if semantic search is available
        """
        if not self.is_available:
            return False
            
        conn = self._conn()
        try:
            # Create virtual table for vector search
            conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_embeddings USING vec0(
                    id INTEGER PRIMARY KEY,
                    content_type TEXT,
                    content_id INTEGER,
                    project_id INTEGER,
                    embedding FLOAT[{EMBEDDING_DIM}]
                )
            """)
            conn.commit()
            self._initialized = True
            return True
        except Exception as e:
            print(f"Warning: Could not initialize embeddings table: {e}")
            return False
        finally:
            conn.close()
    
    def generate_embedding(self, text: str) -> Optional[list[float]]:
        """
        Generate an embedding for the given text.
        
        Args:
            text: Text to embed
            
        Returns:
            List of floats (embedding vector) or None if not available
        """
        if not EMBEDDINGS_AVAILABLE or self.model is None:
            return None
            
        try:
            embedding = self.model.encode(text, convert_to_numpy=True)
            return embedding.tolist()
        except Exception as e:
            print(f"Warning: Could not generate embedding: {e}")
            return None
    
    def store_embedding(
        self,
        content_type: str,
        content_id: int,
        project_id: int,
        text: str
    ) -> bool:
        """
        Generate and store an embedding for content.
        
        Args:
            content_type: Type of content ('problem', 'solution', 'learning', 'change')
            content_id: ID of the content
            project_id: Project ID
            text: Text to embed
            
        Returns:
            True if successful
        """
        if not self.is_available:
            return False
            
        embedding = self.generate_embedding(text)
        if embedding is None:
            return False
            
        conn = self._conn()
        try:
            # Delete existing embedding for this content
            conn.execute(
                "DELETE FROM memory_embeddings WHERE content_type = ? AND content_id = ?",
                (content_type, content_id)
            )
            
            # Insert new embedding
            conn.execute(
                """INSERT INTO memory_embeddings 
                   (content_type, content_id, project_id, embedding) 
                   VALUES (?, ?, ?, ?)""",
                (content_type, content_id, project_id, json.dumps(embedding))
            )
            conn.commit()
            return True
        except Exception as e:
            print(f"Warning: Could not store embedding: {e}")
            return False
        finally:
            conn.close()
    
    def search_similar(
        self,
        query: str,
        project_id: Optional[int] = None,
        content_types: Optional[list[str]] = None,
        limit: int = 10
    ) -> list[dict]:
        """
        Search for similar content using vector similarity.
        
        Args:
            query: Search query
            project_id: Optional project filter
            content_types: Optional list of content types to search
            limit: Maximum results
            
        Returns:
            List of results with similarity scores
        """
        if not self.is_available:
            return []
            
        query_embedding = self.generate_embedding(query)
        if query_embedding is None:
            return []
            
        conn = self._conn()
        try:
            # Build query with filters
            sql = """
                SELECT content_type, content_id, project_id,
                       vec_distance_cosine(embedding, ?) as distance
                FROM memory_embeddings
                WHERE 1=1
            """
            params = [json.dumps(query_embedding)]
            
            if project_id is not None:
                sql += " AND project_id = ?"
                params.append(project_id)
                
            if content_types:
                placeholders = ",".join("?" * len(content_types))
                sql += f" AND content_type IN ({placeholders})"
                params.extend(content_types)
                
            sql += f" ORDER BY distance ASC LIMIT ?"
            params.append(limit)
            
            results = conn.execute(sql, params).fetchall()
            
            return [
                {
                    "content_type": r["content_type"],
                    "content_id": r["content_id"],
                    "project_id": r["project_id"],
                    "similarity": 1 - r["distance"]  # Convert distance to similarity
                }
                for r in results
            ]
        except Exception as e:
            print(f"Warning: Vector search failed: {e}")
            return []
        finally:
            conn.close()
    
    def delete_embedding(self, content_type: str, content_id: int) -> bool:
        """
        Delete an embedding.
        
        Args:
            content_type: Type of content
            content_id: ID of the content
            
        Returns:
            True if successful
        """
        if not self.is_available:
            return False
            
        conn = self._conn()
        try:
            conn.execute(
                "DELETE FROM memory_embeddings WHERE content_type = ? AND content_id = ?",
                (content_type, content_id)
            )
            conn.commit()
            return True
        except Exception as e:
            print(f"Warning: Could not delete embedding: {e}")
            return False
        finally:
            conn.close()
    
    def reindex_all(self, db) -> int:
        """
        Reindex all content in the database.
        
        Args:
            db: Database instance with content to index
            
        Returns:
            Number of items indexed
        """
        if not self.is_available:
            return 0
            
        count = 0
        conn = self._conn()
        
        try:
            # Index problems
            problems = conn.execute(
                "SELECT p.id, p.title, p.description, c.project_id "
                "FROM problems p JOIN components c ON p.component_id = c.id"
            ).fetchall()
            
            for p in problems:
                text = f"{p['title']} {p['description'] or ''}"
                if self.store_embedding("problem", p["id"], p["project_id"], text):
                    count += 1
            
            # Index solutions
            solutions = conn.execute(
                """SELECT s.id, s.summary, s.key_insight, c.project_id 
                   FROM solutions s 
                   JOIN problems p ON s.problem_id = p.id 
                   JOIN components c ON p.component_id = c.id"""
            ).fetchall()
            
            for s in solutions:
                text = f"{s['summary']} {s['key_insight'] or ''}"
                if self.store_embedding("solution", s["id"], s["project_id"], text):
                    count += 1
            
            # Index learnings
            learnings = conn.execute(
                "SELECT id, insight, context, project_id FROM learnings"
            ).fetchall()
            
            for l in learnings:
                text = f"{l['insight']} {l['context'] or ''}"
                if self.store_embedding("learning", l["id"], l["project_id"], text):
                    count += 1
            
            # Index changes
            changes = conn.execute(
                """SELECT ch.id, ch.field_name, ch.old_value, ch.new_value, ch.reason, c.project_id
                   FROM changes ch JOIN components c ON ch.component_id = c.id"""
            ).fetchall()
            
            for ch in changes:
                text = f"{ch['field_name']} {ch['old_value'] or ''} {ch['new_value'] or ''} {ch['reason'] or ''}"
                if self.store_embedding("change", ch["id"], ch["project_id"], text):
                    count += 1
                    
            return count
            
        finally:
            conn.close()


# Singleton instance
_embedding_service: Optional[EmbeddingService] = None


def get_embedding_service(db_path: str) -> EmbeddingService:
    """Get or create the embedding service singleton."""
    global _embedding_service
    if _embedding_service is None or _embedding_service.db_path != db_path:
        _embedding_service = EmbeddingService(db_path)
        _embedding_service.initialize()
    return _embedding_service
