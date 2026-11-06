# -*- coding: utf-8 -*-
"""
检索模块：手写 BM25 关键词检索 + BM25/向量混合检索。【★核心模块·可复用】
BM25 擅长专业名词精确命中（如课程代码 SW301），向量检索擅长语义相近，双路互补。
"""

import math

import numpy as np

from app.config import Config
from app.rag.vector_store import tokenize, embed_texts


class BM25Index:
    """经典 BM25：score = Σ IDF(w)·tf·(k1+1) / (tf + k1·(1-b+b·|d|/avgdl))"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_tokens = []   # 每个切片的分词结果
        self.doc_lens = []     # 每个切片的词数
        self.df = {}           # 词 -> 包含它的切片数
        self.avgdl = 0.0       # 平均切片长度

    def fit(self, token_lists: list):
        """用全部切片的分词结果建立统计量。"""
        self.doc_tokens = token_lists
        self.doc_lens = [len(t) for t in token_lists]
        self.avgdl = sum(self.doc_lens) / max(len(token_lists), 1)
        for tokens in token_lists:
            for tok in set(tokens):
                self.df[tok] = self.df.get(tok, 0) + 1
        return self

    def _idf(self, word: str) -> float:
        df = self.df.get(word, 0)
        n = len(self.doc_tokens)
        return math.log((n - df + 0.5) / (df + 0.5) + 1.0)

    def search(self, query: str, top_k: int) -> list:
        """返回 [(切片下标, BM25得分), ...]，按得分降序。"""
        query_tokens = tokenize(query)
        scores = np.zeros(len(self.doc_tokens), dtype=np.float32)
        for tok in query_tokens:
            idf = self._idf(tok)
            if idf <= 0:
                continue
            for i, tokens in enumerate(self.doc_tokens):
                tf = tokens.count(tok)
                if tf == 0:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * self.doc_lens[i] / self.avgdl)
                scores[i] += idf * tf * (self.k1 + 1) / denom
        top_idx = np.argsort(scores)[::-1][:top_k]
        return [(int(i), float(scores[i])) for i in top_idx if scores[i] > 0]


def hybrid_search(query: str, bm25: BM25Index, faiss_index,
                  encoder, chunks: list, top_k: int = Config.TOP_K) -> list:
    """
    混合检索：BM25 与向量两路分数各自 min-max 归一化后按 0.5:0.5 加权融合，
    取总分最高的 top_k 个切片。
    返回: [{"chunk": 切片原文, "filename": 来源文件名, "score": 融合分}, ...]
    """
    # 通道 1：BM25 关键词
    bm25_scores = dict(bm25.search(query, top_k=len(chunks)))
    # 通道 2：向量语义
    query_vec = embed_texts([query], encoder)[0].reshape(1, -1)
    sims, idxs = faiss_index.search(query_vec, len(chunks))
    vec_scores = {int(i): float(s) for s, i in zip(sims[0], idxs[0]) if i != -1}

    def normalize(score_dict: dict) -> dict:
        """min-max 归一化到 [0,1]，把两路分数拉到同一量纲。"""
        if not score_dict:
            return {}
        vals = list(score_dict.values())
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-9:
            return {k: 0.5 for k in score_dict}
        return {k: (v - lo) / (hi - lo) for k, v in score_dict.items()}

    bm25_norm = normalize(bm25_scores)
    vec_norm = normalize(vec_scores)

    fused = {}
    for i in set(bm25_norm) | set(vec_norm):
        fused[i] = 0.5 * bm25_norm.get(i, 0.0) + 0.5 * vec_norm.get(i, 0.0)

    top = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:top_k]
    return [
        {"chunk": chunks[i]["content"], "filename": chunks[i]["filename"], "score": round(s, 4)}
        for i, s in top
    ]
