"""Raw TTS generation. Nothing else in the pipeline imports Kokoro.

A backend is anything with:
    id           str  - goes into the cache key, so changing voice/lang invalidates audio
    sample_rate  int
    synth(text, speed) -> float32 numpy array (mono)

A backend MAY also implement:
    synth_timed(text, speed) -> (audio, tokens | None)
    close() -> None
where tokens are [(text, start_s, end_s), ...] in the RAW audio, used for
word-level timing. Returning None for tokens is fine: the pipeline then
aligns the words on the audio itself.

Keeping this behind a tiny interface means swapping the engine later (another
local model, a cloud voice) is one new class, not a rewrite.
"""
from __future__ import annotations

import gc
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple

import numpy as np
import torch

SPACY_MODEL_NAME = "en_core_web_sm"


class TTSBackend(Protocol):
    id: str
    sample_rate: int

    def synth(self, text: str, speed: float) -> np.ndarray: ...


def ensure_spacy_model_installed(model_name: str = SPACY_MODEL_NAME) -> None:
    """Verify the spaCy model required by Kokoro/misaki is installed locally and
    disable spacy.cli.download so runtime auto-downloads can never happen."""
    if importlib.util.find_spec(model_name) is None:
        raise RuntimeError(
            f"Required spaCy model '{model_name}' is not installed. "
            f"Runtime auto-download is disabled to prevent memory spikes and network hangs. "
            f"Install it once via: pip install -r requirements.txt"
        )
    try:
        import spacy.cli  # type: ignore

        def _blocked_download(model: str, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(
                f"Blocked runtime auto-download of spaCy model '{model}'. "
                f"Install it explicitly via: pip install -r requirements.txt"
            )

        spacy.cli.download = _blocked_download
    except ImportError:
        pass


class KokoroBackend:
    """Same calls as the original narrate.py:
        KPipeline(lang_code=LANG)
        pipeline(text, voice=VOICE, speed=speed, split_pattern=...)  -> (graphemes, phonemes, audio)
    The model is loaded lazily, on the first segment that actually needs synthesis,
    so a fully cached rebuild never pays the load time.
    """

    sample_rate = 24000
    def __init__(self, voice: str = "default", device: Optional[str] = None):
        self.voice = voice
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self._model = None

    @property
    def id(self) -> str:
        return f"kokoro:{self.voice}:{self.lang}"

    def _get_pipeline(self):
        if self._pipeline is None:
            ensure_spacy_model_installed(SPACY_MODEL_NAME)
            try:
                from kokoro import KPipeline
            except ImportError as e:               # pragma: no cover - needs the real package
                raise RuntimeError("The 'kokoro' package is not installed in this environment "
                                   "(it is installed on your machine; use --backend fake to dry-run "
                                   "elsewhere).") from e
            self._pipeline = KPipeline(lang_code=self.lang)
            if torch.cuda.is_available() and hasattr(self._pipeline, "model"):
                self._pipeline.model = self._pipeline.model.to("cuda")
        return self._pipeline

    def close(self) -> None:
        """Release PyTorch/Kokoro references in-process (note: full OS page
        reclamation requires running narration in a subprocess)."""
        if self._pipeline is not None:
            self._pipeline = None
            gc.collect()
            torch_mod = sys.modules.get("torch")
            if torch_mod is not None and hasattr(torch_mod, "cuda") and torch_mod.cuda.is_available():
                torch_mod.cuda.empty_cache()

    def synth(self, text: str, speed: float) -> np.ndarray:
        return self.synth_timed(text, speed)[0]

    def synth_timed(self, text: str, speed: float) -> Tuple[np.ndarray, Optional[List[Tuple[str, float, float]]]]:
        """Synthesize, collecting per-token timestamps when the installed
        Kokoro version exposes them (Result.tokens with start_ts/end_ts,
        relative to each yielded chunk). Older versions yield plain
        (graphemes, phonemes, audio) triples and tokens come back None - the
        pipeline then falls back to audio alignment.
        """
        pipeline = self._get_pipeline()
        parts: List[np.ndarray] = []
        tokens: Optional[List[Tuple[str, float, float]]] = []
        t = 0.0
        for result in pipeline(text, voice=self.voice, speed=speed, split_pattern=self.split_pattern):
            audio = np.asarray(result[2], dtype=np.float32)
            parts.append(audio)
            if tokens is not None:
                chunk_tokens = getattr(result, "tokens", None)
                if not chunk_tokens:
                    tokens = None                       # this Kokoro has no timestamps
                else:
                    for tok in chunk_tokens:
                        start = getattr(tok, "start_ts", None)
                        end = getattr(tok, "end_ts", None)
                        if start is None or end is None:
                            tokens = None
                            break
                        tokens.append((str(getattr(tok, "text", "")), t + float(start), t + float(end)))
            t += len(audio) / self.sample_rate
        if not parts:
            return np.zeros(0, dtype=np.float32), None
        return np.concatenate(parts), tokens

class ChatterboxTurboBackend:
    """Chatterbox Turbo (chatterbox.tts_turbo.ChatterboxTurboTTS) local CPU backend.
    Loads lazily on first uncached synthesis and frees PyTorch references on close().
    """

    sample_rate = 24000

    def __init__(self, voice: str = "default", device: str = "cpu"):
        self.voice = voice
        self.device = device
        self._model = None

    @property
    def id(self) -> str:
        return f"chatterbox_turbo:{self.voice}"

    def _get_model(self):
        if self._model is None:
            try:
                from chatterbox.tts_turbo import ChatterboxTurboTTS
            except ImportError as e:               # pragma: no cover - needs chatterbox-tts
                raise RuntimeError(
                    "The 'chatterbox-tts' package is not installed. "
                    "Install it via: pip install chatterbox-tts"
                ) from e
            self._model = ChatterboxTurboTTS.from_pretrained(device=self.device)
            sr = getattr(self._model, "sr", None)
            if sr:
                self.sample_rate = int(sr)
        return self._model

    def close(self) -> None:
        if self._model is not None:
            self._model = None
            gc.collect()
            torch_mod = sys.modules.get("torch")
            if torch_mod is not None and hasattr(torch_mod, "cuda") and torch_mod.cuda.is_available():
                torch_mod.cuda.empty_cache()

    def synth(self, text: str, speed: float) -> np.ndarray:
        model = self._get_model()
        kwargs: Dict[str, Any] = {}
        if self.voice and self.voice != "default" and Path(self.voice).is_file():
            kwargs["audio_prompt_path"] = self.voice
        wav = model.generate(text, **kwargs)
        if hasattr(wav, "detach"):
            wav = wav.detach().cpu().numpy()
        return np.asarray(wav, dtype=np.float32).squeeze()


def get_backend(name: str, cfg: Dict[str, Any]) -> TTSBackend:
    tts = cfg["tts"]
    if name == "kokoro":
        backend = KokoroBackend(tts["voice"], tts["lang"], tts.get("split_pattern", r"\n+"))
    elif name in ("chatterbox_turbo", "chatterbox.tts_turbo"):
       # backend = ChatterboxTurboBackend(voice=tts.get("voice", "default"))
        backend = ChatterboxTurboBackend(voice=tts.get("voice", "default"), device=tts.get("device"))
    elif name == "fake":
        from .fake import FakeBackend
        backend = FakeBackend(sample_rate=int(tts["sample_rate"]))
    else:
        raise ValueError(
            f'Unknown TTS backend "{name}". Available: kokoro, chatterbox_turbo, fake'
        )
    if int(tts["sample_rate"]) != backend.sample_rate:
        raise ValueError(f'tts.sample_rate is {tts["sample_rate"]} but backend "{name}" produces '
                         f"{backend.sample_rate} Hz")
    return backend
