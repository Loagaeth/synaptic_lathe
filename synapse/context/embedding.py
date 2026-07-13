"""Embedding 接口 — 本地模型与多云端 API 统一入口。"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from enum import Enum

import httpx

from synapse.config import MemoryConfig
from synapse.logging import synapse_logger


class EmbeddingStatus(Enum):
    OK = "ok"
    NOT_CONFIGURED = "not_configured"
    MODEL_LOAD_FAILED = "model_load_failed"
    API_CALL_FAILED = "api_call_failed"


_local_model = None
_local_model_name = ""
_local_failures: dict[str, float] = {}
_local_lock = threading.Lock()
_RETRY_COOLDOWN = 300  # 5 分钟后允许重试
_MAX_EMBEDDING_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_EMBEDDING_DIMENSIONS = 4096
_MAX_FLOAT32 = 3.4028235e38


def _load_local_model(model_name: str):
    """Load one local model while holding a lock entirely inside a worker thread."""

    global _local_model, _local_model_name
    name = model_name or "all-MiniLM-L6-v2"
    with _local_lock:
        if _local_model is not None and _local_model_name == name:
            return _local_model
        failed_at = _local_failures.get(name, 0.0)
        if failed_at and time.monotonic() - failed_at < _RETRY_COOLDOWN:
            return None
        try:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(name)
        except Exception as exc:
            synapse_logger.warning("Failed to load local embedding model: %s", exc)
            _local_failures[name] = time.monotonic()
            return None
        _local_model = model
        _local_model_name = name
        _local_failures.pop(name, None)
        return model


async def _get_local_model(model_name: str):
    return await asyncio.to_thread(_load_local_model, model_name)


async def _post_json_bounded(client, url: str, **kwargs):
    """POST JSON without allowing an upstream to fill process memory."""

    if hasattr(client, "stream"):
        async with client.stream("POST", url, **kwargs) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    declared_size = 0
                if declared_size > _MAX_EMBEDDING_RESPONSE_BYTES:
                    raise ValueError("Embedding response is too large")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > _MAX_EMBEDDING_RESPONSE_BYTES:
                    raise ValueError("Embedding response is too large")
        return json.loads(body)

    # Small injected clients used by tests and embedders may expose only post().
    response = await client.post(url, **kwargs)
    response.raise_for_status()
    return response.json()


# ── OpenAI / NVIDIA ───────────────────────────


async def _openai_compat_embed(texts: list[str], config: MemoryConfig) -> list[list[float]] | None:
    """OpenAI 兼容 API，覆盖 OpenAI / NVIDIA 及其他兼容服务。"""
    base = config.embedding_api_url or _DEFAULT_URLS.get(config.embedding_provider, "")
    url = base.rstrip("/")
    if not url.endswith("/embeddings"):
        url += "/embeddings"
    key = config.embedding_api_key.get_secret_value()
    body: dict = {"input": texts, "model": config.embedding_model}
    if config.embedding_dimensions:
        body["dimensions"] = config.embedding_dimensions
    # NVIDIA 特有参数
    if config.embedding_provider == "nvidia":
        body["input_type"] = "passage"
        body["encoding_format"] = "float"
    async with httpx.AsyncClient(timeout=config.embedding_timeout, trust_env=config.embedding_trust_env) as c:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        payload = await _post_json_bounded(c, url, json=body, headers=headers)
        data = payload["data"]
        if isinstance(data, list) and all(
            isinstance(item, dict) and isinstance(item.get("index"), int) for item in data
        ):
            data = sorted(data, key=lambda item: item["index"])
            if [item["index"] for item in data] != list(range(len(texts))):
                raise ValueError("Embedding provider returned invalid vector indexes")
        return [item["embedding"] for item in data]


# ── Gemini ────────────────────────────────────


async def _gemini_embed(texts: list[str], config: MemoryConfig) -> list[list[float]] | None:
    try:
        from google import genai
    except ImportError:
        synapse_logger.warning("google-genai not installed: pip install google-genai")
        return None
    key = config.embedding_api_key.get_secret_value()
    client = genai.Client(api_key=key)
    model = config.embedding_model
    model_name = model if model.startswith("models/") else f"models/{model}"
    kwargs: dict = {}
    if config.embedding_dimensions:
        kwargs["output_dimensionality"] = config.embedding_dimensions
    result = await client.aio.models.embed_content(
        model=model_name,
        contents=texts,
        config=kwargs or None,
    )
    return [emb.values for emb in result.embeddings]


# ── Ollama ────────────────────────────────────


async def _ollama_embed(texts: list[str], config: MemoryConfig) -> list[list[float]] | None:
    base = config.embedding_api_url or _DEFAULT_URLS["ollama"]
    url = base.rstrip("/")
    if not url.endswith("/api/embed"):
        url += "/api/embed"
    body: dict = {"model": config.embedding_model, "input": texts}
    if config.embedding_dimensions:
        body["dimensions"] = config.embedding_dimensions
    async with httpx.AsyncClient(timeout=config.embedding_timeout, trust_env=config.embedding_trust_env) as c:
        payload = await _post_json_bounded(c, url, json=body)
        return payload["embeddings"]


# ── Provider 默认 API URL ──

_DEFAULT_URLS = {
    "openai": "https://api.openai.com/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "ollama": "http://127.0.0.1:11434",
}

# ── Handler 注册 ──
_EMBED_HANDLERS = {
    "openai": _openai_compat_embed,
    "nvidia": _openai_compat_embed,
    "gemini": _gemini_embed,
    "ollama": _ollama_embed,
}


# ── Helpers ───────────────────────────────────


def _normalize_embeddings(
    raw: object,
    expected_count: int,
    expected_dimensions: int = 0,
) -> list[list[float]]:
    if not isinstance(raw, (list, tuple)) or len(raw) != expected_count:
        raise ValueError("Embedding provider returned an unexpected number of vectors")
    vectors: list[list[float]] = []
    dimensions = expected_dimensions
    for raw_vector in raw:
        try:
            vector = [float(value) for value in raw_vector]
        except (TypeError, ValueError) as exc:
            raise ValueError("Embedding provider returned a non-numeric vector") from exc
        if not vector or not all(math.isfinite(value) and abs(value) <= _MAX_FLOAT32 for value in vector):
            raise ValueError("Embedding provider returned an empty or invalid float32 vector")
        if len(vector) > _MAX_EMBEDDING_DIMENSIONS:
            raise ValueError("Embedding provider returned an oversized vector")
        if dimensions == 0:
            dimensions = len(vector)
        if len(vector) != dimensions:
            raise ValueError("Embedding provider returned inconsistent dimensions")
        vectors.append(vector)
    return vectors


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


# ── Public API ────────────────────────────────


async def get_embedding(
    text: str,
    config: MemoryConfig,
) -> tuple[list[float] | None, EmbeddingStatus]:
    """单条 embedding，返回 (向量列表, 状态码)。"""
    results, status = await get_embeddings([text], config)
    return (results[0] if results else None, status)


async def get_embeddings(
    texts: list[str],
    config: MemoryConfig,
) -> tuple[list[list[float]] | None, EmbeddingStatus]:
    """批量获取 embeddings。返回 (向量列表, 状态码)。"""
    if not texts:
        return ([], EmbeddingStatus.OK)
    handler = _EMBED_HANDLERS.get(config.embedding_provider)
    # 如果配置了 API URL，强制使用 OpenAI 兼容处理器（覆盖 provider 默认值）
    if not handler and config.embedding_api_url:
        handler = _openai_compat_embed
    if handler:
        try:
            result = await handler(texts, config)
            normalized = _normalize_embeddings(result, len(texts), config.embedding_dimensions)
            return (normalized, EmbeddingStatus.OK)
        except Exception as e:
            synapse_logger.warning("%s embedding API call failed: %s", config.embedding_provider, e)
            return (None, EmbeddingStatus.API_CALL_FAILED)

    if config.embedding_provider in ("local", ""):
        model = await _get_local_model(config.embedding_model)
        if model is None:
            return (None, EmbeddingStatus.MODEL_LOAD_FAILED)
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, lambda: [model.encode(t).tolist() for t in texts])
            normalized = _normalize_embeddings(result, len(texts), config.embedding_dimensions)
            return (normalized, EmbeddingStatus.OK)
        except Exception as e:
            synapse_logger.warning("Local embedding failed: %s", e)
            return (None, EmbeddingStatus.MODEL_LOAD_FAILED)

    synapse_logger.warning("Unknown embedding provider: %s", config.embedding_provider)
    return (None, EmbeddingStatus.NOT_CONFIGURED)


async def search_similar(
    query: str, candidates: list[str], config: MemoryConfig, top_k: int = 5
) -> tuple[list[tuple[int, str, float]], EmbeddingStatus]:
    """语义搜索。返回 ([(index, text, score)], status)。status 非 OK 时调用方应 fallback。"""
    if not candidates:
        return ([], EmbeddingStatus.OK)
    all_texts = [query] + candidates
    embeddings, status = await get_embeddings(all_texts, config)
    if not embeddings:
        synapse_logger.info("Embedding unavailable: %s", status.value)
        return ([], status)
    query_emb = embeddings[0]
    if not any(query_emb):
        synapse_logger.warning("Query embedding is zero vector, check model/provider")
        return ([], EmbeddingStatus.NOT_CONFIGURED)
    scores = [(i, candidates[i], _cosine(query_emb, embeddings[i + 1])) for i in range(len(candidates))]
    scores.sort(key=lambda x: x[2], reverse=True)
    return (scores[:top_k], status)
