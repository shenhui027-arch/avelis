"""混合检索：纯标准库词法 TF-IDF（词元 + 中文单字/双字）与可选 OpenAI 兼容向量相似度融合。

未配置向量端点、网络异常或 Redis 不可用时，自动降级为确定性词法排序，不中断检索主链路。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
from collections import Counter, OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass

import httpx
from redis.exceptions import RedisError

from avelis import cache
from avelis.config import get_settings

logger = logging.getLogger(__name__)

_ASCII_WORD_RE = re.compile(r"[a-z0-9_]+")
_ASCII_SUBWORD_RE = re.compile(r"[a-z0-9]{2,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_UNICODE_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)

_EMB_CACHE_MAX = 2048
_EMB_CACHE_TTL_SECONDS = 7 * 86400
_EMB_BATCH_SIZE = 64
_MIN_SEMANTIC_COSINE = 0.35
_EMB_LOCK = threading.Lock()
_EMB_LRU: OrderedDict[str, list[float]] = OrderedDict()


@dataclass(frozen=True)
class SearchDocument:
    key: str
    fields: list[tuple[float, str]]
    semantic_text: str = ""
    exact_tokens: tuple[str, ...] = ()


def clear_embedding_cache() -> None:
    """清空进程内向量 LRU 缓存（供测试隔离使用）。"""
    with _EMB_LOCK:
        _EMB_LRU.clear()


def tokenize(text: str) -> list[str]:
    """提取词元：英文/数字/下划线词元、拆分短词元、其他字母词元以及中文单字与二元组。"""
    lowered = (text or "").casefold()
    if not lowered:
        return []
    tokens: list[str] = []
    ascii_seen: set[str] = set()
    for word in _ASCII_WORD_RE.findall(lowered):
        tokens.append(word)
        ascii_seen.add(word)
        if "_" in word:
            tokens.extend(sub for sub in _ASCII_SUBWORD_RE.findall(word.replace("_", " ")) if sub != word)
    for run in _CJK_RUN_RE.findall(lowered):
        tokens.extend(run)
        if len(run) > 1:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    for word in _UNICODE_WORD_RE.findall(lowered):
        if word in ascii_seen or _CJK_RUN_RE.fullmatch(word):
            continue
        if not all("a" <= ch <= "z" for ch in word):
            tokens.append(word)
    return tokens


def _normalize_vector(vec: Sequence[float]) -> list[float]:
    values = [float(x) for x in vec]
    norm = math.sqrt(sum(x * x for x in values))
    if norm <= 0.0:
        return values
    return [x / norm for x in values]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _tfidf_cosine(q_weights: dict[str, float], q_norm: float, tokens: list[str], idf: dict[str, float]) -> float:
    if not tokens or q_norm <= 0.0:
        return 0.0
    counts = Counter(tokens)
    dot = 0.0
    doc_sq = 0.0
    for tok, tf in counts.items():
        w = tf * idf.get(tok, 1.0)
        doc_sq += w * w
        if tok in q_weights:
            dot += q_weights[tok] * w
    if dot <= 0.0 or doc_sq <= 0.0:
        return 0.0
    return dot / (q_norm * math.sqrt(doc_sq))


def tfidf_scores(query: str, docs: Sequence[SearchDocument]) -> dict[str, float]:
    """基于文档频率 IDF 与字段加权余弦的词法相似度，归一化至 [0.0, 1.0+]（保持字段权重与平局序）。"""
    q_tokens = tokenize(query)
    if not q_tokens or not docs:
        return {doc.key: 0.0 for doc in docs}

    doc_tokens_list: list[list[list[str]]] = []
    df: Counter[str] = Counter()
    max_field_weight = 1.0
    for doc in docs:
        field_toks: list[list[str]] = []
        seen_in_doc: set[str] = set()
        for weight, text in doc.fields:
            if weight > max_field_weight:
                max_field_weight = weight
            toks = tokenize(text)
            field_toks.append(toks)
            seen_in_doc.update(toks)
        doc_tokens_list.append(field_toks)
        df.update(seen_in_doc)

    total_docs = len(docs)
    idf: dict[str, float] = {tok: math.log((1.0 + total_docs) / (1.0 + freq)) + 1.0 for tok, freq in df.items()}

    q_counts = Counter(q_tokens)
    q_weights = {tok: tf * idf.get(tok, 1.0) for tok, tf in q_counts.items()}
    q_norm = math.sqrt(sum(w * w for w in q_weights.values()))

    scores: dict[str, float] = {}
    for doc, field_toks in zip(docs, doc_tokens_list):
        raw = 0.0
        for (weight, _), toks in zip(doc.fields, field_toks):
            if weight <= 0.0 or not toks:
                continue
            raw += weight * _tfidf_cosine(q_weights, q_norm, toks, idf)
        scores[doc.key] = raw / max_field_weight
    return scores


def _emb_cache_key(model: str, text: str) -> str:
    digest = hashlib.sha256(f"{model}\0{text}".encode("utf-8")).hexdigest()
    return f"avelis:emb:{digest}"


def _lru_get(key: str) -> list[float] | None:
    with _EMB_LOCK:
        vec = _EMB_LRU.get(key)
        if vec is not None:
            _EMB_LRU.move_to_end(key)
        return vec


def _lru_put(key: str, vec: list[float]) -> None:
    with _EMB_LOCK:
        _EMB_LRU[key] = vec
        _EMB_LRU.move_to_end(key)
        while len(_EMB_LRU) > _EMB_CACHE_MAX:
            _EMB_LRU.popitem(last=False)


def embed_texts(texts: Sequence[str], *, settings=None) -> list[list[float]] | None:
    """调用 OpenAI 兼容 /embeddings 端点并缓存单位向量；未配置、网络失败或 Redis 异常时返回 None。"""
    if not texts:
        return []
    cfg = settings or get_settings()
    base_url = (getattr(cfg, "embedding_base_url", "") or "").strip().rstrip("/")
    model = (getattr(cfg, "embedding_model", "") or "").strip()
    if not base_url or not model:
        return None

    api_key = getattr(cfg, "embedding_api_key", "") or ""
    timeout = max(int(getattr(cfg, "embedding_timeout_seconds", 5) or 5), 1)
    keys = [_emb_cache_key(model, t) for t in texts]
    vectors: list[list[float] | None] = [_lru_get(k) for k in keys]

    missing_indices = [i for i, v in enumerate(vectors) if v is None]
    if missing_indices:
        try:
            raw_list = cache.client().mget([keys[i] for i in missing_indices])
            for idx, raw in zip(missing_indices, raw_list):
                if raw:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list) and parsed:
                        vec = _normalize_vector(parsed)
                        vectors[idx] = vec
                        _lru_put(keys[idx], vec)
        except (RedisError, ValueError, TypeError):
            logger.debug("Redis 向量缓存读取失败，继续计算")

    still_missing = [i for i, v in enumerate(vectors) if v is None]
    if still_missing:
        # 同一批内相同文本只嵌入一次，并按 _EMB_BATCH_SIZE 分块请求，避免一次发送过大请求。
        unique_indices_by_text: dict[str, list[int]] = {}
        for idx in still_missing:
            unique_indices_by_text.setdefault(texts[idx], []).append(idx)
        unique_texts = list(unique_indices_by_text.keys())
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            to_redis: dict[str, list[float]] = {}
            for start in range(0, len(unique_texts), _EMB_BATCH_SIZE):
                chunk = unique_texts[start : start + _EMB_BATCH_SIZE]
                resp = httpx.post(
                    f"{base_url}/embeddings",
                    json={"model": model, "input": chunk},
                    headers=headers,
                    timeout=timeout,
                )
                resp.raise_for_status()
                payload = resp.json()
                if not isinstance(payload, dict):
                    return None
                data = payload.get("data")
                if not isinstance(data, list) or len(data) != len(chunk):
                    return None
                if any(not isinstance(item, dict) for item in data):
                    return None
                ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
                for text_item, item in zip(chunk, ordered):
                    emb = item.get("embedding")
                    if not isinstance(emb, list) or not emb:
                        return None
                    vec = _normalize_vector(emb)
                    for target_idx in unique_indices_by_text[text_item]:
                        vectors[target_idx] = vec
                        _lru_put(keys[target_idx], vec)
                        to_redis[keys[target_idx]] = vec
            if to_redis:
                try:
                    pipe = cache.client().pipeline()
                    for k, v in to_redis.items():
                        pipe.set(k, json.dumps(v), ex=_EMB_CACHE_TTL_SECONDS)
                    pipe.execute()
                except RedisError:
                    logger.debug("Redis 向量缓存写入失败，已跳过")
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            logger.debug("向量嵌入请求失败，降级使用本地词法排序")
            return None

    if any(v is None for v in vectors):
        return None
    return [v for v in vectors if v is not None]


def _has_exact_hit(query_lower: str, doc: SearchDocument) -> bool:
    if not query_lower:
        return False
    for _, text in doc.fields:
        if text and query_lower in text.casefold():
            return True
    return any(tok and query_lower in tok.casefold() for tok in doc.exact_tokens)


def hybrid_rank(
    query: str,
    docs: Sequence[SearchDocument],
    *,
    lexical_weight: float = 0.45,
    semantic_weight: float = 0.55,
    exact_boost: float = 0.25,
    settings=None,
) -> list[tuple[str, float]]:
    """融合词法 TF-IDF、精确子串加分与可选向量相似度，按得分降序（平局保持输入序）返回 [(key, score)]。"""
    if not docs:
        return []
    q_stripped = (query or "").strip()
    if not q_stripped or not tokenize(q_stripped):
        return [(doc.key, 0.0) for doc in docs]

    lex = tfidf_scores(q_stripped, docs)
    q_lower = q_stripped.casefold()

    semantic_texts = [q_stripped] + [
        doc.semantic_text or " ".join(text for _, text in doc.fields if text) for doc in docs
    ]
    embeddings = embed_texts(semantic_texts, settings=settings)

    scored: list[tuple[float, int, str, float]] = []
    if embeddings is not None and len(embeddings) == len(docs) + 1:
        q_vec, doc_vecs = embeddings[0], embeddings[1:]
        for idx, (doc, d_vec) in enumerate(zip(docs, doc_vecs)):
            lex_score = lex.get(doc.key, 0.0)
            raw_cos = cosine_similarity(q_vec, d_vec)
            sem_score = raw_cos if raw_cos >= _MIN_SEMANTIC_COSINE else 0.0
            boost = exact_boost if _has_exact_hit(q_lower, doc) else 0.0
            total = lexical_weight * lex_score + semantic_weight * sem_score + boost
            scored.append((-round(total, 9), idx, doc.key, total))
    else:
        for idx, doc in enumerate(docs):
            lex_score = lex.get(doc.key, 0.0)
            boost = exact_boost if _has_exact_hit(q_lower, doc) else 0.0
            total = lex_score + boost
            scored.append((-round(total, 9), idx, doc.key, total))

    scored.sort(key=lambda item: (item[0], item[1]))
    return [(key, total) for _, _, key, total in scored]
