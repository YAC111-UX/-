# -*- coding: utf-8 -*-
"""
RAG 流水线：应用启动时构建一次，全局复用。【★核心模块·可复用】
阶段 5 改造：数据源从"直接读 md 文件"切换为 kb_chunks 表（enabled=1），
新增 rebuild()：管理员增删/启停文档后全量重建索引（试点数据量小，秒级完成）。
"""

import time
import threading

import faiss
import numpy as np

from app.config import Config
from app.rag.kb_store import load_active_chunks
from app.rag.vector_store import TfidfEncoder, tokenize, embed_texts
from app.rag.retriever import BM25Index


class RagPipeline:
    """检索流水线单例：构建期读库建索引，请求期只做查询、不重建索引。"""

    def __init__(self):
        t0 = time.time()

        # 1. 从数据库读取启用切片（kb_chunks.enabled=1 且文档 status=active）
        chunks = load_active_chunks()

        # 2. 分词结果提前算好（TF-IDF 训练与 BM25 索引共用，避免查询期重复分词）
        token_lists = [tokenize(c["content"]) for c in chunks]

        # 3. 向量化 + FAISS 内积索引（向量已归一化，内积=余弦相似度）
        self.encoder = None
        if Config.EMBEDDING_PROVIDER != "openai":
            self.encoder = TfidfEncoder().fit(token_lists)
        if chunks:
            chunk_vectors = embed_texts([c["content"] for c in chunks], self.encoder)
            self.dim = chunk_vectors.shape[1]
            self.faiss_index = faiss.IndexFlatIP(self.dim)
            self.faiss_index.add(np.asarray(chunk_vectors, dtype=np.float32))
        else:
            # 知识库被清空（如全部停用）：维度占位为 0，search 直接返回空
            self.dim = 0
            self.faiss_index = None

        # 4. BM25 索引
        self.bm25 = BM25Index().fit(token_lists)

        # 5. 切片列表挂到实例上（hybrid_search 需要按下标取回切片内容）
        self.chunks = chunks

        print(f"[RAG] 知识库就绪：{len(chunks)} 个切片，"
              f"向量维度 {self.dim}，构建耗时 {time.time() - t0:.1f} 秒")

    def search(self, question: str, top_k: int = Config.TOP_K) -> list:
        """对一个问题做混合检索，返回 top_k 个片段。"""
        if not self.chunks:              # 知识库为空 → 制度中查不到任何条文
            return []
        from app.rag.retriever import hybrid_search
        return hybrid_search(question, self.bm25, self.faiss_index,
                             self.encoder, self.chunks, top_k)


# 模块级单例：main.py 启动时赋值，全应用共用
pipeline: RagPipeline = None

# 重建状态标志：rebuild() 期间为 True，此时问答请求返回 2003「知识库正在维护」
rebuilding = False
_rebuild_lock = threading.Lock()


def rebuild() -> None:
    """
    全量重建 RAG 索引（管理员上传/启停/删除文档后调用）。
    加锁串行化，避免两个管理员同时上传导致并发重建；重建完成后原子替换单例。
    """
    global pipeline, rebuilding
    with _rebuild_lock:
        rebuilding = True
        try:
            pipeline = RagPipeline()
        finally:
            rebuilding = False
