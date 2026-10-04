"""Text -> L2-normalised 384-d vectors.

One code path for training and serving (same ONNX file, same tokenizer), so there is no
train/serve skew. `HashEmbedder` is an offline stand-in for tests and CI only.
"""

from __future__ import annotations

import hashlib
import unicodedata
from pathlib import Path
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from .config import EMBED_DIM, Settings

Vecs = NDArray[np.float32]


def normalize_text(text: str) -> str:
    """NFC + collapsed whitespace. Applied before hashing and before embedding."""
    return " ".join(unicodedata.normalize("NFC", text).split())


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> Vecs: ...


class OnnxEmbedder:
    def __init__(self, model_path: Path, tokenizer_path: Path, max_tokens: int = 128, batch_size: int = 64) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.tok = Tokenizer.from_file(str(tokenizer_path))
        self.tok.enable_truncation(max_length=max_tokens)
        self.tok.enable_padding(pad_id=1, pad_token="<pad>")  # noqa: S106 - XLM-R pad token, not a secret
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(str(model_path), opts, providers=["CPUExecutionProvider"])
        self.input_names = {i.name for i in self.session.get_inputs()}
        self.batch_size = batch_size

    def embed(self, texts: list[str]) -> Vecs:
        out = np.empty((len(texts), EMBED_DIM), dtype=np.float32)
        # Sort by length so each batch pads to a similar width (~2x throughput on mixed-length feeds).
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        for start in range(0, len(order), self.batch_size):
            idx = order[start : start + self.batch_size]
            # e5 models are trained with a task prefix; "query: " is the symmetric/classification one.
            enc = self.tok.encode_batch(["query: " + normalize_text(texts[i]) for i in idx])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.input_names:
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self.session.run(None, feeds)[0]
            m = mask[:, :, None].astype(np.float32)
            pooled = (hidden * m).sum(1) / m.sum(1)
            out[idx] = pooled / np.linalg.norm(pooled, axis=1, keepdims=True)
        return out


class HashEmbedder:
    """Character-trigram feature hashing. Deterministic, dependency-free, NOT multilingual."""

    def embed(self, texts: list[str]) -> Vecs:
        out = np.zeros((len(texts), EMBED_DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            t = f"  {normalize_text(text).lower()}  "
            for i in range(len(t) - 2):
                h = int.from_bytes(hashlib.blake2b(t[i : i + 3].encode(), digest_size=5).digest(), "big")
                out[row, h % EMBED_DIM] += 1.0 if (h >> 39) & 1 else -1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return (out / np.where(norms == 0, 1.0, norms)).astype(np.float32)


def get_embedder(settings: Settings) -> Embedder:
    if settings.embed_backend == "hash":
        return HashEmbedder()
    from .fetch import fetch_embed_model

    model, tok = fetch_embed_model(settings.data_dir)
    return OnnxEmbedder(model, tok)
