"""
知识库服务：sqlite-vec 语义检索 + L1/L2 两级 embedding 缓存。

⚠️ 此服务所有方法为同步，必须通过 run_in_thread 包裹后在 async 环境调用。
"""
import sqlite3
import json
import hashlib
import logging
import numpy as np
from pathlib import Path
from typing import List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

# 全局知识库服务单例（启动时初始化一次）
_knowledge_service_instance = None


def get_knowledge_service() -> "KnowledgeService":
    global _knowledge_service_instance
    if _knowledge_service_instance is None:
        _knowledge_service_instance = KnowledgeService()
    return _knowledge_service_instance


class KnowledgeService:
    """
    sqlite-vec 语义检索知识库，无需独立向量数据库。
    支持 L1（内存）+ L2（SQLite 持久化）两级 embedding 缓存。
    """

    def __init__(self, db_path: str = "monica_knowledge.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        try:
            import sqlite_vec
            # 优先使用 load_entrypoint（无需 enable_load_extension，兼容 macOS 系统 SQLite）
            if hasattr(sqlite_vec, "load_entrypoint"):
                self.conn.create_function("vec_version", 0, lambda: "ok")  # placeholder
                sqlite_vec.load_entrypoint(self.conn)
            else:
                self.conn.enable_load_extension(True)
                sqlite_vec.load(self.conn)
                self.conn.enable_load_extension(False)
            self._vec_available = True
        except Exception as e:
            logger.warning(f"[Knowledge] sqlite-vec 加载失败，向量检索不可用（降级为关键词检索）: {e}")
            self._vec_available = False
        self._init_schema()
        self._embedding_cache: dict = {}   # L1 内存缓存

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_items (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT,
                title    TEXT,
                content  TEXT,
                source   TEXT
            )
        """)
        if self._vec_available:
            self.conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_vec
                USING vec0(
                    item_id   INTEGER PRIMARY KEY,
                    embedding FLOAT[1536]
                )
            """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS embedding_cache (
                text_hash  TEXT PRIMARY KEY,
                embedding  BLOB NOT NULL,
                created_at REAL DEFAULT (unixepoch())
            )
        """)
        self.conn.commit()

    def _embed(self, text: str) -> np.ndarray:
        """
        获取 text 的 embedding 向量。
        优先级：L1 内存缓存 → L2 SQLite 缓存 → OpenAI API
        """
        text_hash = hashlib.sha256(text.encode()).hexdigest()

        # L1 缓存
        if text_hash in self._embedding_cache:
            return self._embedding_cache[text_hash]

        # L2 缓存
        row = self.conn.execute(
            "SELECT embedding FROM embedding_cache WHERE text_hash = ?",
            (text_hash,)
        ).fetchone()
        if row:
            vec = np.frombuffer(row[0], dtype=np.float32)
            self._embedding_cache[text_hash] = vec
            return vec

        # 调用 OpenAI API（同步 httpx，供 run_in_thread 使用）
        import httpx
        resp = httpx.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            json={"model": "text-embedding-3-small", "input": text[:4096]},
            timeout=30,
        )
        resp.raise_for_status()
        vec = np.array(resp.json()["data"][0]["embedding"], dtype=np.float32)

        # 双写：内存 + SQLite
        self._embedding_cache[text_hash] = vec
        self.conn.execute(
            "INSERT OR REPLACE INTO embedding_cache (text_hash, embedding) VALUES (?, ?)",
            (text_hash, vec.tobytes())
        )
        self.conn.commit()
        return vec

    def search(
        self,
        query: str,
        top_k: int = 3,
        category: Optional[str] = None,
    ) -> List[dict]:
        """语义检索，返回最相关的知识条目"""
        if not self._vec_available:
            return self._fallback_keyword_search(query, top_k, category)

        query_vec = self._embed(query)
        vec_json  = json.dumps(query_vec.tolist())

        cat_filter = "AND k.category = ?" if category else ""
        params: list = [vec_json]
        if category:
            params.append(category)
        params.append(top_k)

        rows = self.conn.execute(f"""
            SELECT k.id, k.category, k.title, k.content, k.source, v.distance
            FROM knowledge_vec v
            JOIN knowledge_items k ON k.id = v.item_id
            WHERE v.embedding MATCH ?
              AND k.rowid IS NOT NULL
              {cat_filter}
            ORDER BY v.distance
            LIMIT ?
        """, params).fetchall()

        return [
            {
                "id":         r[0],
                "category":   r[1],
                "title":      r[2],
                "content":    r[3],
                "source":     r[4],
                "similarity": round(1 - r[5], 4),
            }
            for r in rows
        ]

    def _fallback_keyword_search(
        self, query: str, top_k: int, category: Optional[str]
    ) -> List[dict]:
        """sqlite-vec 不可用时的关键词降级检索"""
        cat_filter = "AND category = ?" if category else ""
        params: list = [f"%{query}%", f"%{query}%"]
        if category:
            params.append(category)
        params.append(top_k)

        rows = self.conn.execute(f"""
            SELECT id, category, title, content, source
            FROM knowledge_items
            WHERE (title LIKE ? OR content LIKE ?)
              {cat_filter}
            LIMIT ?
        """, params).fetchall()

        return [
            {"id": r[0], "category": r[1], "title": r[2],
             "content": r[3], "source": r[4], "similarity": 0.5}
            for r in rows
        ]

    def ingest_jsonl(self, jsonl_path: str):
        """将知识库 jsonl 文件 embedding 后写入 sqlite-vec"""
        path = Path(jsonl_path)
        if not path.exists():
            logger.warning(f"[Knowledge] {jsonl_path} 不存在，跳过导入")
            return

        count = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    text = item.get("title", "") + " " + item.get("content", "")
                    vec  = self._embed(text)

                    cursor = self.conn.execute(
                        "INSERT INTO knowledge_items (category, title, content, source) "
                        "VALUES (?,?,?,?)",
                        (
                            item.get("category", ""),
                            item.get("title", ""),
                            item.get("content", ""),
                            item.get("source", ""),
                        ),
                    )
                    item_id = cursor.lastrowid

                    if self._vec_available:
                        self.conn.execute(
                            "INSERT OR REPLACE INTO knowledge_vec (item_id, embedding) "
                            "VALUES (?, ?)",
                            [item_id, json.dumps(vec.tolist())],
                        )
                    count += 1
                except Exception as e:
                    logger.warning(f"[Knowledge] 导入条目失败: {e}")

        self.conn.commit()
        logger.info(f"[Knowledge] 导入完成，共 {count} 条，来源: {jsonl_path}")

    def is_empty(self) -> bool:
        count = self.conn.execute(
            "SELECT COUNT(*) FROM knowledge_items"
        ).fetchone()[0]
        return count == 0

    def ensure_loaded(
        self,
        cases_path: str = "knowledge_base/cases.jsonl",
        guidelines_path: str = "knowledge_base/guidelines.jsonl",
    ):
        """冷启动保障：若知识库为空则自动导入"""
        if self.is_empty():
            logger.warning("[Knowledge] 知识库为空，开始导入...")
            for path in [cases_path, guidelines_path]:
                self.ingest_jsonl(path)

    def get_stats(self) -> dict:
        total = self.conn.execute(
            "SELECT COUNT(*) FROM knowledge_items"
        ).fetchone()[0]
        cache_size = len(self._embedding_cache)
        return {
            "total_items": total,
            "cache_size":  cache_size,
            "vec_enabled": self._vec_available,
        }
