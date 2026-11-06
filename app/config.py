# -*- coding: utf-8 -*-
"""全局配置：全部来自环境变量（.env），禁止把密钥写进代码。【★核心模块·可复用】"""

import os
from pathlib import Path

# 读取项目根目录的 .env 配置文件
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass


class Config:
    """全局配置对象：LLM / Embedding / 知识库 / 数据库"""

    # ---- LLM 大模型（OpenAI 兼容接口，换供应商只改 .env，不改代码） ----
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    LLM_API_KEY = os.getenv("LLM_API_KEY", "")
    LLM_MODEL = os.getenv("LLM_MODEL", "glm-4-flash")
    LLM_TIMEOUT = 60  # 秒

    # ---- Embedding 向量化 ----
    EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "tfidf").lower()  # openai / tfidf
    EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", LLM_BASE_URL)
    EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", LLM_API_KEY)
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "embedding-3")

    # ---- 知识库 ----
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    KB_DIR = PROJECT_ROOT / "data" / "kb_source"   # 制度文档目录
    CHUNK_SIZE = 500                               # 业务约定：500 字符切片
    CHUNK_OVERLAP = 50                             # 相邻切片重叠，防止条文被截断
    TOP_K = 3                                      # 混合检索取 top3

    # ---- 数据库 ----
    DB_PATH = PROJECT_ROOT / "data" / "app.db"     # SQLite 文件

    # ---- 业务参数 ----
    HISTORY_ROUNDS = 6        # 每次请求携带最近 6 轮对话作为上下文
    MAX_QUESTION_LEN = 500    # 提问最大长度
    CITATION_EXCERPT_LEN = 80  # 引用来源片段节选长度
