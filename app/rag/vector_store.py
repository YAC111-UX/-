# -*- coding: utf-8 -*-
"""
向量化封装：统一出口 embed_texts()，底层支持两种模式。【★核心模块·可复用】
  openai = 调用 OpenAI 兼容向量化接口（效果最好，需要 Key）
  tfidf  = 本地 TF-IDF 词袋向量（零成本离线可用，用于无 Key 时跑通流程）
"""

import numpy as np

from app.config import Config


def tokenize(text: str) -> list:
    """中文分词：jieba 精确模式 + 过滤空白符。TF-IDF 与 BM25 共用。"""
    import jieba
    return [w for w in jieba.lcut(text) if w.strip()]


class TfidfEncoder:
    """本地 TF-IDF 编码器（离线兜底方案）。"""

    def __init__(self):
        self.vocab = {}    # 词 -> 列下标
        self.idf = None    # 每列 IDF 权重

    def fit(self, token_lists: list):
        """用语料库构建词表与 IDF。"""
        for tokens in token_lists:
            for tok in tokens:
                if tok not in self.vocab:
                    self.vocab[tok] = len(self.vocab)
        n_docs = len(token_lists)
        df = np.zeros(len(self.vocab))
        for tokens in token_lists:
            for tok in set(tokens):          # 同一文档内重复词只计一次
                df[self.vocab[tok]] += 1
        # 平滑 IDF：越稀有的词区分度越大
        self.idf = np.log(n_docs / (df + 1.0)) + 1.0
        return self

    def transform(self, tokens: list) -> np.ndarray:
        """词列表 -> L2 归一化 TF-IDF 向量（内积即余弦相似度）。"""
        vec = np.zeros(len(self.vocab), dtype=np.float32)
        for tok in tokens:
            idx = self.vocab.get(tok)
            if idx is not None:
                vec[idx] += 1.0              # 词频 TF
        vec *= self.idf
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        return vec


def embed_texts_api(texts: list) -> np.ndarray:
    """调用 OpenAI 兼容 Embedding 接口，批量向量化并 L2 归一化。"""
    import requests

    url = f"{Config.EMBEDDING_BASE_URL.rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {Config.EMBEDDING_API_KEY}"}
    resp = requests.post(
        url, headers=headers,
        json={"model": Config.EMBEDDING_MODEL, "input": texts},
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Embedding 接口返回 {resp.status_code}")
    vectors = [item["embedding"] for item in resp.json()["data"]]
    arr = np.array(vectors, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return arr / np.clip(norms, 1e-10, None)


def embed_texts(texts: list, encoder: TfidfEncoder = None) -> np.ndarray:
    """统一向量化入口：上层不关心底层是 API 还是 TF-IDF。"""
    if Config.EMBEDDING_PROVIDER == "openai":
        return embed_texts_api(texts)
    return np.vstack([encoder.transform(tokenize(t)) for t in texts])
