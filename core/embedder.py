"""
SigLIP 2 / CLIP embedding engine for visual-semantic search.
Generates dense vector embeddings from screen images and text queries
for cosine similarity-based retrieval.

Uses ONNX Runtime with DirectML for AMD GPU acceleration.
Falls back to CPU if DirectML unavailable.
"""
import os
import struct
import time
import logging
from typing import Optional, List, Tuple
from pathlib import Path
from PIL import Image
import numpy as np

logger = logging.getLogger(__name__)

# Model configurations
MODELS = {
    "siglip2-base": {
        "repo": "google/siglip2-base-patch16-224",
        "dim": 768,
        "image_size": 224,
    },
    "clip-vit-base": {
        "repo": "openai/clip-vit-base-patch32",
        "dim": 512,
        "image_size": 224,
    },
}


class EmbeddingEngine:
    """
    Generates visual and text embeddings using SigLIP 2 or CLIP.
    Optimized for AMD GPU via ONNX Runtime DirectML provider.
    """

    def __init__(self, model_name: str = "siglip2-base",
                 models_dir: str = "models",
                 prefer_gpu: bool = True):
        self.model_name = model_name
        self.models_dir = models_dir
        self.config = MODELS.get(model_name, MODELS["clip-vit-base"])
        self.embedding_dim = self.config["dim"]
        self.image_size = self.config["image_size"]

        self._model = None
        self._processor = None
        self._tokenizer = None
        self._provider = None
        self._backend = None
        self._device = None
        self._initialized = False
        self._init_error: Optional[str] = None

        self._init_model(prefer_gpu)

    def _init_model(self, prefer_gpu: bool):
        """Initialize the embedding model."""
        errors = []
        # Try transformers + ONNX Runtime first
        try:
            self._init_transformers(prefer_gpu)
            return
        except Exception as e:
            errors.append(f"transformers: {e}")
            logger.warning(f"Transformers init failed: {e}")
            # Clean up partial state from failed init
            self._model = None
            self._processor = None
            self._device = None

        # Fallback: use Ollama embeddings
        try:
            self._init_ollama_embeddings()
            return
        except Exception as e:
            errors.append(f"ollama: {e}")
            logger.warning(f"Ollama embeddings init failed: {e}")

        self._init_error = "; ".join(errors)
        logger.error(f"No embedding backend available: {self._init_error}")

    def _ensure_initialized(self, operation: str = "embed"):
        """Raise RuntimeError if the model is not loaded. Call before any embed operation."""
        if not self._initialized:
            msg = f"EmbeddingEngine.{operation}() called but no model is loaded"
            if self._init_error:
                msg += f" (init errors: {self._init_error})"
            raise RuntimeError(msg)
    # signed: gamma

    def _init_transformers(self, prefer_gpu: bool):
        """Initialize using HuggingFace Transformers."""
        from transformers import AutoProcessor, AutoModel
        import torch

        device = "cpu"
        if prefer_gpu:
            if torch.cuda.is_available():
                device = "cuda"
            else:
                try:
                    import torch_directml
                    device = torch_directml.device()
                    logger.info("Using DirectML (AMD GPU)")
                except ImportError:
                    logger.info("DirectML not available, using CPU")

        model_id = self.config["repo"]
        logger.info(f"Loading {model_id} on {device}")

        self._processor = AutoProcessor.from_pretrained(model_id)
        self._model = AutoModel.from_pretrained(model_id).to(device).eval()
        self._device = device
        self._backend = "transformers"
        self._initialized = True
        logger.info(f"Embedding model loaded: {model_id} ({self.embedding_dim}D)")

    def _init_ollama_embeddings(self):
        """Use Ollama's embedding endpoint as fallback."""
        import urllib.request
        # Test connection
        req = urllib.request.Request("http://localhost:11434/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = __import__("json").loads(resp.read())

        self._backend = "ollama"
        self._initialized = True
        logger.info("Using Ollama for text embeddings (image embeddings disabled)")

    def embed_image(self, image: Image.Image) -> Optional[np.ndarray]:
        """Generate embedding vector from an image.

        Raises RuntimeError if no embedding backend is loaded.
        """
        self._ensure_initialized("embed_image")

        start = time.perf_counter()
    # signed: gamma

        if self._backend == "transformers":
            return self._embed_image_transformers(image, start)
        elif self._backend == "ollama":
            # Ollama doesn't support image embeddings directly
            # Use a text description instead (requires prior VLM analysis)
            return None

        return None

    def _embed_image_transformers(self, image: Image.Image, start: float) -> np.ndarray:
        """Generate image embedding using Transformers model."""
        import torch

        # Preprocess
        inputs = self._processor(images=image, return_tensors="pt")
        inputs = {k: v.to(self._device) for k, v in inputs.items()}

        # Forward pass
        with torch.no_grad():
            outputs = self._model.get_image_features(**inputs)
            embedding = outputs.cpu().numpy().flatten()

        # Normalize
        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm

        elapsed = (time.perf_counter() - start) * 1000
        logger.debug(f"Image embedding: {elapsed:.0f}ms ({self.embedding_dim}D)")
        return embedding

    def embed_text(self, text: str) -> Optional[np.ndarray]:
        """Generate embedding vector from text (for search queries).

        Raises RuntimeError if no embedding backend is loaded.
        """
        self._ensure_initialized("embed_text")

        start = time.perf_counter()
    # signed: gamma

        if self._backend == "transformers":
            return self._embed_text_transformers(text, start)
        elif self._backend == "ollama":
            return self._embed_text_ollama(text, start)

        return None

    def _embed_text_transformers(self, text: str, start: float) -> np.ndarray:
        """Generate text embedding using Transformers model."""
        import torch

        inputs = self._processor(text=text, return_tensors="pt", padding=True)
        inputs = {k: v.to(self._device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = self._model.get_text_features(**inputs)
            embedding = outputs.cpu().numpy().flatten()

        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm

        elapsed = (time.perf_counter() - start) * 1000
        logger.debug(f"Text embedding: {elapsed:.0f}ms")
        return embedding

    def _embed_text_ollama(self, text: str, start: float) -> np.ndarray:
        """Generate text embedding using Ollama embed API."""
        import urllib.request
        import json

        payload = json.dumps({
            "model": "qwen3:8b",
            "input": text,
        }).encode("utf-8")

        req = urllib.request.Request(
            "http://localhost:11434/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            embeddings = data.get("embeddings", [[]])
            embedding = np.array(embeddings[0], dtype=np.float32)

        # Resize to match expected dim if needed
        if len(embedding) != self.embedding_dim:
            # Project to target dimension
            if len(embedding) > self.embedding_dim:
                embedding = embedding[: self.embedding_dim]
            else:
                embedding = np.pad(embedding, (0, self.embedding_dim - len(embedding)))

        norm = np.linalg.norm(embedding)
        if norm > 0:
            embedding = embedding / norm

        elapsed = (time.perf_counter() - start) * 1000
        logger.debug(f"Ollama text embedding: {elapsed:.0f}ms ({len(embedding)}D)")
        return embedding

    def embed_batch(self, images: List[Image.Image]) -> List[Optional[np.ndarray]]:
        """Batch embed multiple images for efficiency.

        Raises RuntimeError if no embedding backend is loaded.
        Uses single-image fallback for non-transformers backends.
        """
        self._ensure_initialized("embed_batch")
        if self._backend != "transformers":
            return [self.embed_image(img) for img in images]
    # signed: gamma

        import torch

        inputs = self._processor(images=images, return_tensors="pt", padding=True)
        inputs = {k: v.to(self._device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = self._model.get_image_features(**inputs)
            embeddings = outputs.cpu().numpy()

        # Normalize each
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = np.where(norms > 0, norms, 1)
        embeddings = embeddings / norms

        return list(embeddings)

    @staticmethod
    def serialize(embedding: np.ndarray) -> bytes:
        """Serialize numpy embedding to bytes for database storage."""
        return struct.pack(f"{len(embedding)}f", *embedding.tolist())

    @staticmethod
    def deserialize(data: bytes, dim: int = 768) -> np.ndarray:
        """Deserialize bytes to numpy embedding."""
        return np.array(struct.unpack(f"{dim}f", data), dtype=np.float32)

    @staticmethod
    def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        """Compute cosine similarity between two embeddings."""
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))

    @property
    def is_available(self) -> bool:
        return self._initialized


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    engine = EmbeddingEngine(prefer_gpu=True)
    print(f"Backend: {engine._backend}")
    print(f"Dimension: {engine.embedding_dim}")
    print(f"Available: {engine.is_available}")

    if engine.is_available:
        # Test text embedding
        emb = engine.embed_text("VS Code editor with Python code")
        if emb is not None:
            print(f"Text embedding shape: {emb.shape}")
            print(f"Text embedding sample: {emb[:5]}")

            # Test serialization
            serialized = engine.serialize(emb)
            deserialized = engine.deserialize(serialized, len(emb))
            print(f"Serialization roundtrip OK: {np.allclose(emb, deserialized)}")
