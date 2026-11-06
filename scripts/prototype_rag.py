# -*- coding: utf-8 -*-
"""
知行职业学院排课选课助手 · 阶段 1：裸 RAG 命令行问答原型
=================================================================

本脚本是【单文件本地原型】，不涉及任何 Web 服务，用于在写 FastAPI 之前
先跑通「文档切片 → 向量化入库 → BM25+向量混合检索 → LLM 回答」整条链路。

模块复用标记说明（重要）：
  标注【★核心模块·可复用】的部分，在阶段 2（FastAPI Web 服务）和
  阶段 3（Agent 智能体）中会被原样拆出复用，不改逻辑。
  标注【阶段1专用】的部分（命令行交互），后续会被 FastAPI 接口替代后删除。

运行前准备：
  1. pip install -r requirements.txt
  2. 把 .env.example 复制为 .env，填入你的 LLM API Key
  3. python scripts/prototype_rag.py

支持两种向量化模式（.env 中 EMBEDDING_PROVIDER 切换）：
  openai = 调用 OpenAI 兼容向量化接口（效果最好，需要 Key）
  tfidf  = 本地 TF-IDF 词袋向量（零成本离线跑通，用于演示与自测）
"""

import os
import sys
import math
import time
import argparse
from pathlib import Path

import requests
import numpy as np
import jieba
import faiss

# dotenv 用于读取项目根目录的 .env 配置文件（API Key 等敏感信息不写进代码）
try:
    from dotenv import load_dotenv
except ImportError:
    # 没装 python-dotenv 也能跑：直接读系统环境变量
    def load_dotenv(*args, **kwargs):
        return False


# =========================================================================
# 第 0 部分：配置加载
# =========================================================================

# 项目根目录 = 本脚本所在目录(scripts/)的上一级
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 加载 .env（存在则加载；不存在则只用系统环境变量）
load_dotenv(PROJECT_ROOT / ".env")


class Config:
    """全局配置对象：全部来自环境变量，方便切换模型供应商【★核心模块·可复用】"""

    # ---- LLM 大模型（OpenAI 兼容接口，换供应商只需改 .env，不改代码） ----
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    LLM_API_KEY = os.getenv("LLM_API_KEY", "")
    LLM_MODEL = os.getenv("LLM_MODEL", "glm-4-flash")
    LLM_TIMEOUT = 60  # 秒，超时直接报错，不做静默重试

    # ---- Embedding 向量化 ----
    EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "tfidf").lower()
    EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", LLM_BASE_URL)
    EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", LLM_API_KEY)
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "embedding-3")

    # ---- 知识库 ----
    KB_DIR = PROJECT_ROOT / "data" / "kb_source"   # 制度文档目录（markdown）
    CHUNK_SIZE = 500                               # 业务约定：500 字符切片
    CHUNK_OVERLAP = 50                             # 相邻切片重叠 50 字符，避免条文被拦腰截断
    TOP_K = 3                                      # 业务约定：混合检索取 top3 片段


# 兜底话术：知识库里查不到时必须说的话，严禁编造（来自需求清单红线）
FALLBACK_ANSWER = "【制度中未找到对应说明，请联系排课专员人工核实】"


# =========================================================================
# 第 1 部分：LLM 调用封装【★核心模块·可复用】
# -------------------------------------------------------------------------
# 独立的纯函数，只依赖 Config，与命令行/知识库逻辑零耦合。
# 阶段 2 会原样搬进 app/core/llm.py，阶段 3 的 Agent 也调用它。
# =========================================================================

def chat_with_llm(system_prompt: str, user_prompt: str) -> str:
    """
    调用 OpenAI 兼容接口的大模型，返回回答文本。

    参数:
        system_prompt: 系统提示词（角色设定 + 约束 + 检索到的制度条文上下文）
        user_prompt:   用户本次提问
    返回:
        模型回答字符串
    异常:
        ConnectionError      网络不通 / 超时
        RuntimeError         API 返回业务错误（Key 无效、余额不足等）
    """
    if not Config.LLM_API_KEY:
        raise RuntimeError(
            "尚未配置 LLM_API_KEY：请复制 .env.example 为 .env 并填入 API Key"
        )

    url = f"{Config.LLM_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {Config.LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": Config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,   # 教务问答要求严谨，温度调低减少自由发挥
        "stream": False,      # 阶段 1 命令行不搞流式；阶段 2 的 /chat 接口再开 SSE
    }

    try:
        resp = requests.post(
            url, headers=headers, json=payload, timeout=Config.LLM_TIMEOUT
        )
    except requests.RequestException as e:
        # 网络层异常统一转成业务可读错误（阶段 2 会映射成业务错误码）
        raise ConnectionError(f"LLM 接口网络异常：{e}") from e

    if resp.status_code != 200:
        raise RuntimeError(f"LLM 接口返回 {resp.status_code}：{resp.text[:200]}")

    data = resp.json()
    # OpenAI 兼容格式的回答固定在 choices[0].message.content
    return data["choices"][0]["message"]["content"].strip()


# =========================================================================
# 第 2 部分：Embedding 向量化封装【★核心模块·可复用】
# -------------------------------------------------------------------------
# 统一出口：embed_texts(texts) -> np.ndarray，形状 (n, dim)
# 上层代码（FAISS 入库 / 查询向量化）不关心底层是 API 还是 TF-IDF。
# =========================================================================

def _tokenize_for_tfidf(text: str) -> list:
    """中文分词：jieba 精确模式 + 过滤空白符。TF-IDF 和 BM25 共用。"""
    return [w for w in jieba.lcut(text) if w.strip()]


class TfidfEncoder:
    """
    本地 TF-IDF 词袋向量编码器（离线兜底方案）。
    原理：统计每个词在文档中的频率(TF)和区分度(IDF)，把文本变成定长向量。
    效果不如语义模型，但零成本离线可用，足够验证流程。
    """

    def __init__(self):
        self.vocab = {}    # 词 -> 列下标
        self.idf = None    # 每列的 IDF 权重

    def fit(self, token_lists: list):
        """用语料库（全部切片）构建词表和 IDF。"""
        # 先收集词表
        for tokens in token_lists:
            for tok in tokens:
                if tok not in self.vocab:
                    self.vocab[tok] = len(self.vocab)
        # 计算 IDF：越稀有的词权重越大
        n_docs = len(token_lists)
        df = np.zeros(len(self.vocab))
        for tokens in token_lists:
            for tok in set(tokens):          # set：一篇文档内重复词只计一次
                df[self.vocab[tok]] += 1
        # 平滑 IDF，避免除零：log(N/(df+1)) + 1
        self.idf = np.log(n_docs / (df + 1.0)) + 1.0
        return self

    def transform(self, tokens: list) -> np.ndarray:
        """把一篇文章的词列表转成 L2 归一化的 TF-IDF 向量。"""
        vec = np.zeros(len(self.vocab), dtype=np.float32)
        for tok in tokens:
            idx = self.vocab.get(tok)
            if idx is not None:
                vec[idx] += 1.0              # 词频 TF（原始计数）
        vec *= self.idf                      # 乘 IDF 得到 TF-IDF
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm                      # L2 归一化 → 内积即余弦相似度
        return vec

    @property
    def dim(self) -> int:
        return len(self.vocab)


def embed_texts_api(texts: list) -> np.ndarray:
    """调用 OpenAI 兼容的 Embedding 接口，批量向量化。"""
    url = f"{Config.EMBEDDING_BASE_URL.rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {Config.EMBEDDING_API_KEY}"}
    resp = requests.post(
        url,
        headers=headers,
        json={"model": Config.EMBEDDING_MODEL, "input": texts},
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Embedding 接口返回 {resp.status_code}：{resp.text[:200]}")
    vectors = [item["embedding"] for item in resp.json()["data"]]
    arr = np.array(vectors, dtype=np.float32)
    # L2 归一化：配合 FAISS 内积索引 = 余弦相似度
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return arr / np.clip(norms, 1e-10, None)


def embed_texts(texts: list, encoder: TfidfEncoder = None) -> np.ndarray:
    """
    统一向量化入口：
      EMBEDDING_PROVIDER=openai → 调 API
      EMBEDDING_PROVIDER=tfidf  → 本地编码器（需先 fit）
    """
    if Config.EMBEDDING_PROVIDER == "openai":
        return embed_texts_api(texts)
    token_lists = [_tokenize_for_tfidf(t) for t in texts]
    return np.vstack([encoder.transform(t) for t in token_lists])


# =========================================================================
# 第 3 部分：文档加载与切片【★核心模块·可复用】
# =========================================================================

def load_markdown_files(kb_dir: Path) -> list:
    """
    读取目录下全部 .md 制度文档。
    返回: [{"filename": 文件名, "content": 全文}, ...]
    """
    if not kb_dir.exists():
        raise FileNotFoundError(f"知识库目录不存在：{kb_dir}")

    docs = []
    for md_file in sorted(kb_dir.glob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        docs.append({"filename": md_file.name, "content": text})
        print(f"  已读取制度文档：{md_file.name}（{len(text)} 字符）")
    if not docs:
        raise FileNotFoundError(f"知识库目录中没有 .md 文件：{kb_dir}")
    return docs


def chunk_text(text: str, size: int = Config.CHUNK_SIZE,
               overlap: int = Config.CHUNK_OVERLAP) -> list:
    """
    滑动窗口切片：每片最长 size 字符，相邻切片重叠 overlap 字符。
    重叠的作用：制度条文常在 500 字符边界处被截断，重叠片段能保证
    至少有一个切片包含完整条文，提升检索命中率。
    """
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunk = text[start:end].strip()
        if chunk:                      # 跳过纯空白切片
            chunks.append(chunk)
        if end >= len(text):
            break
        start = end - overlap          # 下一片回退 overlap 字符
    return chunks


# =========================================================================
# 第 4 部分：FAISS 向量索引构建【★核心模块·可复用】
# =========================================================================

def build_vector_index(chunk_vectors: np.ndarray):
    """
    用 FAISS 建立内积（IP）索引。
    因为所有向量已 L2 归一化，内积 = 余弦相似度，取值 [0,1]，越大越相似。
    """
    dim = chunk_vectors.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(chunk_vectors)
    return index


# =========================================================================
# 第 5 部分：BM25 关键词检索【★核心模块·可复用】
# -------------------------------------------------------------------------
# 手写 BM25（不引第三方库），算法与经典定义一致：
#   score(q, d) = Σ IDF(w) · tf·(k1+1) / (tf + k1·(1-b+b·|d|/avgdl))
# 作用：向量检索擅长"语义相近"，BM25 擅长"专业名词精确命中"
# （例如课程代码 SW301 这类关键词），两者互补。
# =========================================================================

class BM25Index:
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
        """BM25 的 IDF：词越稀有，权重越高。"""
        df = self.df.get(word, 0)
        n = len(self.doc_tokens)
        return math.log((n - df + 0.5) / (df + 0.5) + 1.0)

    def search(self, query: str, top_k: int) -> list:
        """返回 [(切片下标, BM25得分), ...]，按得分降序。"""
        query_tokens = _tokenize_for_tfidf(query)
        scores = np.zeros(len(self.doc_tokens), dtype=np.float32)
        for tok in query_tokens:
            idf = self._idf(tok)
            if idf <= 0:
                continue
            for i, tokens in enumerate(self.doc_tokens):
                tf = tokens.count(tok)
                if tf == 0:
                    continue
                # BM25 词项得分公式
                denom = tf + self.k1 * (1 - self.b + self.b * self.doc_lens[i] / self.avgdl)
                scores[i] += idf * tf * (self.k1 + 1) / denom
        # 取 top_k 个下标
        top_idx = np.argsort(scores)[::-1][:top_k]
        return [(int(i), float(scores[i])) for i in top_idx if scores[i] > 0]


# =========================================================================
# 第 6 部分：BM25 + 向量混合检索【★核心模块·可复用】
# =========================================================================

def hybrid_search(query: str, bm25: BM25Index, faiss_index,
                  encoder: TfidfEncoder, chunks: list,
                  top_k: int = Config.TOP_K) -> list:
    """
    混合检索流程：
      1) BM25 关键词通道 → 每个切片一个分数
      2) 向量语义通道   → 余弦相似度分数
      3) 两路分数各自做 min-max 归一化到 [0,1]，再按 0.5 : 0.5 加权融合
      4) 取加权总分最高的 top_k 个切片
    返回: [{"chunk": 切片原文, "filename": 来源文件名, "score": 融合分}, ...]
    """
    # ---- 通道 1：BM25 ----
    bm25_hits = bm25.search(query, top_k=len(chunks))
    bm25_scores = {idx: s for idx, s in bm25_hits}

    # ---- 通道 2：向量 ----
    query_vec = embed_texts([query], encoder)[0].reshape(1, -1)
    sims, idxs = faiss_index.search(query_vec, len(chunks))
    vec_scores = {int(i): float(s) for s, i in zip(sims[0], idxs[0]) if i != -1}

    # ---- min-max 归一化（把两路分数拉到同一量纲再相加） ----
    def normalize(score_dict: dict) -> dict:
        if not score_dict:
            return {}
        vals = list(score_dict.values())
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-9:                       # 全部分数相同 → 全部归 0.5
            return {k: 0.5 for k in score_dict}
        return {k: (v - lo) / (hi - lo) for k, v in score_dict.items()}

    bm25_norm = normalize(bm25_scores)
    vec_norm = normalize(vec_scores)

    # ---- 加权融合（0.5/0.5 起步，试点后可用评测用例调权重） ----
    fused = {}
    for i in set(bm25_norm) | set(vec_norm):
        fused[i] = 0.5 * bm25_norm.get(i, 0.0) + 0.5 * vec_norm.get(i, 0.0)

    top = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:top_k]
    return [
        {
            "chunk": chunks[i]["content"],
            "filename": chunks[i]["filename"],
            "score": round(score, 4),
        }
        for i, score in top
    ]


# =========================================================================
# 第 7 部分：系统提示词拼装（阶段 1 专用简化版）
# -------------------------------------------------------------------------
# 阶段 5 的管理后台会把它做成数据库模板，可在线编辑；此处先写死占位。
# =========================================================================

def build_system_prompt(hits: list) -> str:
    """把检索到的制度条文拼进系统提示词，并施加业务红线约束。"""
    context = "\n\n".join(
        f"【来源 {n}】《{h['filename']}》节选：\n{h['chunk']}"
        for n, h in enumerate(hits, start=1)
    )
    return (
        "你是知行职业学院排课选课智能助手，只能依据下面提供的教务制度、培养方案、"
        "场地规则条文回答用户。\n"
        "如果知识库没有对应内容，明确回复：" + FALLBACK_ANSWER + "\n"
        "回答末尾必须附上引用文档来源；严禁编造先修条件、教室容量、实训室占用规则；"
        "不可以自动选课、修改排课计划。\n\n"
        "===== 检索到的制度条文 =====\n" + context + "\n===== 制度条文结束 ====="
    )


def build_citations(hits: list) -> str:
    """生成回答末尾的引用来源清单：文件名 + 片段节选（前 80 字符）。"""
    lines = [
        f"{n}. 《{h['filename']}》片段：{h['chunk'][:80]}…"
        for n, h in enumerate(hits, start=1)
    ]
    return "\n".join(lines)


# =========================================================================
# 第 8 部分：命令行主循环【阶段1专用】
# =========================================================================

class RagPipeline:
    """
    把上面各核心模块串成一条流水线。
    阶段 2 中 answer() 的内部流程会被 FastAPI /chat 接口原样调用。
    """

    def __init__(self):
        # 1. 读取制度文档
        print("[1/4] 读取教务制度文档…")
        docs = load_markdown_files(Config.KB_DIR)

        # 2. 切片
        print("[2/4] 按 500 字符切片…")
        self.chunks = []
        for doc in docs:
            for piece in chunk_text(doc["content"]):
                self.chunks.append({"content": piece, "filename": doc["filename"]})
        print(f"      共切出 {len(self.chunks)} 个片段")

        # 3. 向量化 + FAISS 入库 + BM25 建索引
        print(f"[3/4] 向量化入库（模式：{Config.EMBEDDING_PROVIDER}）…")
        t0 = time.time()
        self.encoder = None
        if Config.EMBEDDING_PROVIDER != "openai":
            # TF-IDF 模式：先用全部切片训练词表
            token_lists = [_tokenize_for_tfidf(c["content"]) for c in self.chunks]
            self.encoder = TfidfEncoder().fit(token_lists)
            chunk_vectors = embed_texts([c["content"] for c in self.chunks], self.encoder)
            # BM25 分词结果提前算好，避免查询时重复分词
            self.bm25 = BM25Index().fit(token_lists)
        else:
            chunk_vectors = embed_texts([c["content"] for c in self.chunks])
            self.bm25 = BM25Index().fit(
                [_tokenize_for_tfidf(c["content"]) for c in self.chunks]
            )
        self.faiss_index = build_vector_index(chunk_vectors)
        print(f"      索引构建完成，耗时 {time.time() - t0:.1f} 秒，向量维度 {chunk_vectors.shape[1]}")

    def answer(self, question: str) -> str:
        """完整问答：检索 top3 → 拼提示词 → 调 LLM → 附引用来源。"""
        hits = hybrid_search(question, self.bm25, self.faiss_index,
                             self.encoder, self.chunks)
        system_prompt = build_system_prompt(hits)
        reply = chat_with_llm(system_prompt, question)
        return reply + "\n\n———— 引用来源 ————\n" + build_citations(hits)

    def search_only(self, question: str) -> list:
        """只检索不调 LLM（自测用，也留给阶段 8 评测脚本复用）。"""
        return hybrid_search(question, self.bm25, self.faiss_index,
                             self.encoder, self.chunks)


def main():
    parser = argparse.ArgumentParser(description="排课选课助手 · 阶段1 裸 RAG 原型")
    parser.add_argument("--search-only", action="store_true",
                        help="只演示混合检索结果，不调用 LLM（无 API Key 时自测用）")
    args = parser.parse_args()

    pipeline = RagPipeline()

    print("\n================ 排课选课助手已就绪 ================")
    print("示例问题：我这门课有没有先修限制？")
    print("          实训室维护期间课程冲突怎么处理？")
    print("输入 q 退出\n")

    while True:
        try:
            question = input("你的问题 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if not question:
            continue
        if question.lower() == "q":
            print("再见！")
            break

        try:
            if args.search_only:
                # 自测模式：展示 top3 检索片段，不花 API 费用
                for n, h in enumerate(pipeline.search_only(question), start=1):
                    print(f"\n—— 检索片段 {n}（融合分 {h['score']}，《{h['filename']}》）——")
                    print(h["chunk"][:200] + ("…" if len(h["chunk"]) > 200 else ""))
            else:
                print("\n助手回答：")
                print(pipeline.answer(question))
        except (ConnectionError, RuntimeError) as e:
            # 统一中文可读错误输出（阶段 2 会映射成业务错误码返回前端）
            print(f"\n[出错了] {e}")
        print()


if __name__ == "__main__":
    sys.exit(main())
