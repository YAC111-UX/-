# -*- coding: utf-8 -*-
"""
============================================================
 报销助手 · 阶段 1 最小原型：裸命令行 RAG 问答
============================================================
功能：
  1. 读取本地报销制度 markdown 文件
  2. 按 ~500 字切片 -> 调用 embedding 接口向量化 -> 存入 FAISS
  3. 命令行输入问题 -> BM25 + 向量混合检索 top3
  4. 命中片段拼入提示词 -> 调用 LLM 生成"带出处"的回答

运行前准备：
  pip install faiss-cpu rank_bm25 jieba openai numpy python-dotenv

  在同目录 .env 文件中配置（任何 OpenAI 兼容供应商均可）：
    LLM_BASE_URL=https://api.deepseek.com/v1
    LLM_API_KEY=sk-xxx
    LLM_CHAT_MODEL=deepseek-chat
    LLM_EMBED_MODEL=text-embedding-v3
    POLICY_FILE=policy_demo.md

============================================================
 模块复用标注（阶段 2 迁移路线）
============================================================
  [核心模块①] call_llm / get_embeddings      -> app/agent/llm/      (LLM 网关)
  [核心模块②] chunk_markdown                 -> app/services/       (知识库切片器)
  [核心模块③] HybridRetriever                -> app/agent/retriever (混合检索器)
  [核心模块④] SYSTEM_PROMPT / build_prompt   -> app/agent/prompts/  (提示词模板)
  [临时胶水]  main() / 命令行循环 / .env 读取  -> 阶段 2 由 FastAPI + 前端替代
============================================================
"""

# ============================================================
# 0. 环境与依赖
# ============================================================
import os
import sys

import numpy as np
import faiss                      # 向量索引库（阶段2生产环境换 pgvector，检索接口逻辑不变）
import jieba                      # 中文分词，供 BM25 使用
from rank_bm25 import BM25Okapi   # 轻量 BM25 实现，无需额外服务
from openai import OpenAI         # 使用 OpenAI SDK 的"兼容模式"访问任意供应商
from dotenv import load_dotenv

# 加载 .env，把配置与代码分离（阶段 2 迁移到 pydantic Settings）
load_dotenv()

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_CHAT_MODEL = os.getenv("LLM_CHAT_MODEL", "deepseek-chat")
LLM_EMBED_MODEL = os.getenv("LLM_EMBED_MODEL", "text-embedding-v3")
POLICY_FILE = os.getenv("POLICY_FILE", "policy_demo.md")

# ---- 原型参数（集中放在顶部，方便调参）----
CHUNK_SIZE = 500      # 每个切片的目标长度（字符）
CHUNK_OVERLAP = 50    # 相邻切片的重叠长度，避免句子被切断后语义丢失
TOP_K = 3             # 最终拼入提示词的片段数
BM25_WEIGHT = 0.4     # 混合检索权重：BM25（关键词精确匹配，如"发票号""滴滴"）
VECTOR_WEIGHT = 0.6   # 混合检索权重：向量（语义相似，如"住宿能报多少"≈"差旅住宿标准"）


# ============================================================
# [核心模块①] LLM 网关：所有大模型调用只走这两个函数
#   阶段 2 直接平移到 app/agent/llm/gateway.py，接口签名不变。
#   供应商可配置：只要对方提供 OpenAI 兼容接口，改 .env 即可切换。
# ============================================================
_client = None


def _get_client() -> OpenAI:
    """懒初始化 SDK 客户端（避免 import 时就要求 API Key 已就绪）"""
    global _client
    if _client is None:
        _client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    return _client


def call_llm(messages: list, temperature: float = 0.2) -> str:
    """
    调用对话模型，返回回答文本。
    messages 格式：[{"role": "system"|"user"|"assistant", "content": "..."}]
    temperature 取低值(0.2)：制度问答要求"忠实引用"，不要发挥。
    """
    resp = _get_client().chat.completions.create(
        model=LLM_CHAT_MODEL,
        messages=messages,
        temperature=temperature,
    )
    return resp.choices[0].message.content


def get_embeddings(texts: list, batch_size: int = 16) -> list:
    """
    批量调用向量模型，返回每段文本的 embedding 列表。
    分批请求：制度文档切片可能有几十上百段，一次全发容易被限流。
    """
    all_vecs = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        resp = _get_client().embeddings.create(model=LLM_EMBED_MODEL, input=batch)
        # 按返回顺序取出向量（API 保证与输入顺序一致）
        all_vecs.extend([item.embedding for item in resp.data])
    return all_vecs


# ============================================================
# [核心模块②] 文档切片器：markdown -> 语义切片列表
#   阶段 2 平移到 app/services/（知识库服务），并增加"按标题分节"策略。
# ============================================================
def _hard_split(text: str, size: int, overlap: int) -> list:
    """处理超长单段：按固定步长硬切，相邻切片保留 overlap 重叠"""
    parts, step = [], size - overlap
    i = 0
    while i < len(text):
        parts.append(text[i:i + size])
        if i + size >= len(text):
            break
        i += step
    return parts


def chunk_markdown(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list:
    """
    切片策略（原型版）：
      1. 先按空行切成自然段（markdown 里空行通常是语义边界）
      2. 逐段累积进缓冲区，超过 chunk_size 就落一个切片
      3. 落切片时带上前一片段的尾部 overlap，保证上下文延续
      4. 单段自身超过 chunk_size 的（如长表格），调用硬切兜底
    返回：[{"chunk_id": 0, "text": "..."}, ...]
    """
    paragraphs = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    chunks, buf = [], ""

    for para in paragraphs:
        # 兜底：单段超长，先冲刷缓冲区，再对该段硬切
        if len(para) > chunk_size:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_hard_split(para, chunk_size, overlap))
            continue

        # 加入当前段后超限 -> 先落盘当前缓冲，再开新缓冲（带尾部重叠）
        if buf and len(buf) + len(para) + 1 > chunk_size:
            chunks.append(buf)
            buf = (buf[-overlap:] + "\n" + para) if overlap > 0 else para
        else:
            buf = (buf + "\n" + para) if buf else para

    if buf:  # 收尾
        chunks.append(buf)

    return [{"chunk_id": i, "text": c} for i, c in enumerate(chunks)]


# ============================================================
# [核心模块③] 混合检索器：BM25（关键词）+ 向量（语义）融合
#   阶段 2 平移到 app/agent/retriever.py，FAISS 换 pgvector 后
#   只需替换 _build_vector_index / _vector_search 两个内部方法。
# ============================================================
def _normalize(scores: dict) -> dict:
    """min-max 归一化到 [0,1]：BM25 和向量余弦分数量纲不同，必须归一后才能加权融合"""
    if not scores:
        return {}
    vals = np.array(list(scores.values()), dtype="float32")
    vmin, vmax = vals.min(), vals.max()
    if vmax - vmin < 1e-9:              # 所有分数相同（如只有1个候选），不拉伸
        return {k: 1.0 for k in scores}
    return {k: float((v - vmin) / (vmax - vmin)) for k, v in scores.items()}


class HybridRetriever:
    """启动时构建一次索引，之后所有提问共享（对应架构中'服务层'的检索器）"""

    def __init__(self, chunks: list):
        self.chunks = chunks
        texts = [c["text"] for c in chunks]

        # ---- 向量侧：embedding -> L2 归一化 -> 内积索引（等价余弦相似度）----
        vecs = np.array(get_embeddings(texts), dtype="float32")
        faiss.normalize_L2(vecs)
        self.index = faiss.IndexFlatIP(vecs.shape[1])
        self.index.add(vecs)

        # ---- BM25 侧：中文分词后建倒排索引 ----
        self.tokenized = [jieba.lcut(t) for t in texts]
        self.bm25 = BM25Okapi(self.tokenized)

    def _vector_search(self, query: str, cand_k: int) -> dict:
        """向量召回：返回 {chunk_id: 相似度分数}"""
        qv = np.array([get_embeddings([query])[0]], dtype="float32")
        faiss.normalize_L2(qv)
        d, i = self.index.search(qv, cand_k)
        return {int(idx): float(s) for s, idx in zip(d[0], i[0]) if idx >= 0}

    def retrieve(self, query: str, top_k: int = TOP_K) -> list:
        """
        混合检索流程：
          1. 两路各自召回更多候选（top_k*3，保证融合后有得选）
          2. 两路分数分别归一化
          3. 加权融合 -> 排序 -> 取 top_k
        返回：[(chunk_dict, 融合分数), ...] 按分数降序
        """
        cand_k = min(len(self.chunks), max(top_k * 3, 10))

        vec_scores = _normalize(self._vector_search(query, cand_k))
        bm25_raw = self.bm25.get_scores(jieba.lcut(query))
        bm25_scores = _normalize({i: float(s) for i, s in enumerate(bm25_raw) if s > 0})

        # 加权融合（两路并集）
        fused = {}
        for cid in set(vec_scores) | set(bm25_scores):
            fused[cid] = (VECTOR_WEIGHT * vec_scores.get(cid, 0.0)
                          + BM25_WEIGHT * bm25_scores.get(cid, 0.0))

        ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)[:top_k]
        return [(self.chunks[cid], score) for cid, score in ranked]


# ============================================================
# [核心模块④] 提示词模板：阶段 2 迁移到 app/agent/prompts/ 独立文件
#   设计要点：
#   - 强制"只依据制度依据回答"，抑制模型编造制度
#   - 要求标注引用的片段编号，前端才能展示"出自《差旅标准》第X节"
#   - 制度没覆盖时明确说"未找到"，宁可拒答不可乱答
# ============================================================
SYSTEM_PROMPT = """你是企业的报销制度问答助手。请严格遵守以下规则：
1. 只依据下方【制度依据】中的内容回答，禁止使用任何外部知识或自行编造制度条款。
2. 回答末尾用【出处：片段X】标注所引用的片段编号；若综合多个片段，逐一标注。
3. 如果【制度依据】中没有相关信息，直接回答"现行制度文档中未找到相关规定，建议咨询财务部"。
4. 回答简洁、分点，金额和条件必须与原文完全一致。"""


def build_prompt(query: str, hits: list) -> list:
    """把检索结果拼装成最终发给 LLM 的 messages（RAG 的'拼上下文'步骤）"""
    context = "\n\n".join(
        f"【片段{c['chunk_id']}】\n{c['text']}" for c, _score in hits
    )
    user_content = f"【制度依据】\n{context}\n\n【问题】\n{query}"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# ============================================================
# [临时胶水] main：阶段 2 由 FastAPI 路由 + 前端对话窗替代
# ============================================================
def main():
    if not LLM_API_KEY:
        sys.exit("缺少 LLM_API_KEY，请在 .env 中配置后重试。")

    # 1. 读文件 -> 切片
    if not os.path.exists(POLICY_FILE):
        sys.exit(f"制度文件不存在：{POLICY_FILE}")
    with open(POLICY_FILE, "r", encoding="utf-8") as f:
        text = f.read()

    chunks = chunk_markdown(text)
    print(f"[初始化] 已读取 {POLICY_FILE}，切分为 {len(chunks)} 个片段")

    # 2. 建索引（embedding + FAISS + BM25）
    retriever = HybridRetriever(chunks)
    print(f"[初始化] 混合索引构建完成，供应商: {LLM_BASE_URL}，模型: {LLM_CHAT_MODEL}")
    print("[就绪] 输入报销问题开始提问，输入 exit 退出\n")

    # 3. 命令行问答循环
    while True:
        try:
            query = input("问题> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not query or query.lower() in ("exit", "quit", "q"):
            break

        hits = retriever.retrieve(query)
        print(f"  [检索命中] " + ", ".join(f"片段{c['chunk_id']}(分{score:.2f})" for c, score in hits))

        answer = call_llm(build_prompt(query, hits))
        print(f"\n回答: {answer}\n")


if __name__ == "__main__":
    main()
