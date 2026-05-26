#!/usr/bin/env python
import argparse
import ctypes
import json
import os
import re
import tempfile
import threading
import time
import queue
import traceback
import wave
import winsound
from dataclasses import dataclass
from pathlib import Path

import keyboard
import numpy as np
import pyautogui
import pyperclip
import sounddevice as sd
from faster_whisper import WhisperModel

try:
    import torch
    HAS_CUDA = torch.cuda.is_available()
except ImportError:
    HAS_CUDA = False

APP_NAME = "Tania Dictée"
APP_MUTEX_NAME = "Local\\TaniaDicteePushToTalk"
APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = APP_DIR / "runtime"
LOG_FILE = RUNTIME_DIR / "events.log"
STATE_FILE = RUNTIME_DIR / "last-state.json"
LAST_TRANSCRIPT_FILE = RUNTIME_DIR / "last-transcript.txt"
DEFAULT_GLOSSARY_FILE = APP_DIR / "glossary.txt"
DEFAULT_GLOSSARY_TERMS = [
    "Tania Dictée",
    "push-to-talk",
    "dashboard",
    "Telegram",
    "Discord",
    "Slack",
]
SOFT_REPLACEMENTS = [
    (r"\btania dictee\b", "Tania Dictée"),
    (r"\bquebec\b", "Québec"),
    (r"\btelegramme\b", "Telegram"),
    (r"\bje suis impressionné\b", "j'ai l'impression"),
    (r"\bje suis impressionnée\b", "j'ai l'impression"),
    # Normalisation I.A. et ia → IA
    (r"I\.A\.", "IA"),
    (r"\bia\b", "IA"),
]
DEFAULT_INITIAL_PROMPT = (
    "Transcription fidèle mot à mot. "
    "Ton naturel québécois ou français, franglais gardé tel quel. "
    "NE JAMAIS adoucir, reformuler, rendre prudent ou poli le contenu. "
    "NE JAMAIS transformer une question en affirmation ni inventer du contenu. "
    "Si un mot est dit en anglais, le garder EN ANGLAIS, ne jamais le traduire "
    "(ex: 'build' reste 'build' pas 'construire', 'ship' reste 'ship'). "
)
DEFAULT_HOTKEY = "f6"
DEFAULT_CLEANUP_MODE = "gentle"
DEFAULT_CANCEL_KEY = "esc"
KEY_ALIASES = {
    "control": "ctrl",
    "return": "enter",
    "escape": "esc",
    "option": "alt",
    "command": "windows",
    "cmd": "windows",
    "win": "windows",
    "super": "windows",
    "spacebar": "space",
}
MODIFIER_KEYS = {"ctrl", "alt", "shift", "windows"}
FUNCTION_KEYS = {f"f{i}" for i in range(1, 25)}
pyautogui.FAILSAFE = True
_APP_MUTEX_HANDLE = None

# --- Auto-detect best device/model combo ---
def auto_detect_device() -> str:
    """Return 'cuda' if a CUDA GPU is available, else 'cpu'."""
    return "cuda" if HAS_CUDA else "cpu"


def auto_detect_compute_type(device: str) -> str:
    """Return optimal compute type for the device."""
    if device == "cuda":
        return "float16"
    return "int8"


def auto_detect_model(device: str) -> str:
    """Return best model for the device. GPU = large-v3, CPU = small."""
    if device == "cuda":
        return "large-v3"
    return "small"


def ensure_runtime_dir():
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def append_runtime_log(message: str):
    try:
        ensure_runtime_dir()
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass


def write_runtime_state(state: str, detail: str = ""):
    try:
        ensure_runtime_dir()
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "state": state,
            "detail": detail,
        }
        STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def write_last_transcript(text: str):
    try:
        ensure_runtime_dir()
        LAST_TRANSCRIPT_FILE.write_text(text or "", encoding="utf-8")
    except Exception:
        pass


def ping(kind: str):
    try:
        if kind == "recording":
            winsound.Beep(960, 55)
        elif kind == "done":
            winsound.Beep(1180, 65)
        elif kind == "ignored":
            winsound.Beep(520, 70)
        elif kind == "error":
            winsound.Beep(320, 120)
        elif kind == "cancelled":
            winsound.Beep(430, 90)
        elif kind == "busy":
            winsound.Beep(700, 60)
    except Exception:
        pass


def acquire_single_instance() -> bool:
    global _APP_MUTEX_HANDLE
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, APP_MUTEX_NAME)
    last_error = kernel32.GetLastError()
    _APP_MUTEX_HANDLE = handle
    # ERROR_ALREADY_EXISTS = 183
    return bool(handle) and int(last_error) != 183


def normalize_key_name(key: str):
    return KEY_ALIASES.get(str(key or "").strip().lower(), str(key or "").strip().lower())


def parse_hotkey_keys(hotkey: str):
    raw = [normalize_key_name(part) for part in str(hotkey or "").split("+") if str(part).strip()]
    seen = []
    for key in raw:
        if key and key not in seen:
            seen.append(key)
    return tuple(seen)


def validate_hotkey_config(hotkey: str, cancel_key: str):
    hotkey_keys = parse_hotkey_keys(hotkey)
    cancel = normalize_key_name(cancel_key)
    if not hotkey_keys:
        raise ValueError(f"Hotkey must not be empty, got: {hotkey!r}")
    single_function_key = len(hotkey_keys) == 1 and hotkey_keys[0] in FUNCTION_KEYS
    if len(hotkey_keys) < 2 and not single_function_key:
        raise ValueError(f"Hotkey must be a combo like ctrl+space or a function key like f6, got: {hotkey!r}")
    if len(hotkey_keys) >= 2 and not any(key in MODIFIER_KEYS for key in hotkey_keys):
        raise ValueError(f"Hotkey combo must include a modifier key (ctrl/alt/shift/windows), got: {hotkey!r}")
    if cancel in hotkey_keys:
        raise ValueError(f"Cancel key must not overlap the push-to-talk hotkey, got cancel={cancel!r} hotkey={hotkey!r}")
    return hotkey_keys, cancel


def load_glossary_terms(glossary_path: str | None):
    terms = []
    seen = set()
    for raw in DEFAULT_GLOSSARY_TERMS:
        key = raw.casefold()
        if key not in seen:
            seen.add(key)
            terms.append(raw)
    if not glossary_path:
        return terms
    path = Path(glossary_path)
    if not path.exists():
        return terms
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            value = line.strip().lstrip("-*").strip()
            if not value or value.startswith("#"):
                continue
            key = value.casefold()
            if key not in seen:
                seen.add(key)
                terms.append(value)
    except Exception as error:
        append_runtime_log(f"glossary-load-failed path={path} error={error}")
    return terms


def build_initial_prompt(extra_terms):
    terms = [str(item).strip() for item in (extra_terms or []) if str(item).strip()]
    if terms:
        return (
            f"{DEFAULT_INITIAL_PROMPT} Favoriser une orthographe propre, les accents usuels, les apostrophes naturelles, "
            f"et une ponctuation simple quand elle est évidente. Garder le franglais utile sans inventer. "
            f"Vocabulaire fréquent et hotwords à privilégier: {', '.join(terms)}."
        )
    return DEFAULT_INITIAL_PROMPT


def apply_soft_replacements(text: str):
    cleaned = (text or "").strip()
    if not cleaned:
        return cleaned
    for pattern, replacement in SOFT_REPLACEMENTS:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def apply_gentle_rewrite(text: str):
    cleaned = (text or "").strip()
    if not cleaned:
        return cleaned

    cleaned = re.sub(r"\s+([,;:.!?])", r"\1", cleaned)
    cleaned = re.sub(r"([,;:.!?])(\S)", r"\1 \2", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # Pas de remplacement de vocabulaire — garder le québécois, le franglais, l'informel tel quel

    cleaned = re.sub(r"\b(https?://\S+)\s+([,;:.!?])", r"\1\2", cleaned)
    cleaned = re.sub(r"(^|[.!?]\s+)([a-zàâçéèêëîïôûùüÿñæœ])", lambda m: m.group(1) + m.group(2).upper(), cleaned)
    if cleaned and cleaned[-1] not in ".!?…":
        cleaned += "."
    return cleaned


LLM_CLEANUP_PROMPT = (
    "Tu es un correcteur de transcription vocale minimaliste.\n"
    "Règles STRICTES — aucune exception :\n"
    "- Retourne UNIQUEMENT le texte corrigé, rien d'autre\n"
    "- ZÉRO commentaire, note, explication ou parenthèse ajoutée par toi\n"
    "- Corrige UNIQUEMENT les mots clairement mal transcrits par Whisper (noms propres, termes techniques)\n"
    "- Ajoute la ponctuation de base si absente\n"
    "- GARDE absolument le vocabulaire québécois et l'argot : gars, les gars, checker, chill, rush, etc.\n"
    "- GARDE le style, les expressions et le registre de langue exactement tels quels\n"
    "- NE reformule JAMAIS, NE traduis JAMAIS, NE soutiens JAMAIS le niveau de langue\n"
    "- Si le texte est compréhensible tel quel, ne touche à rien sauf la ponctuation\n"
    "Transcription à corriger :\n{text}"
)
DEFAULT_LLM_MODEL = "mistral"
DEFAULT_LLM_URL = "http://localhost:11434/api/generate"


def llm_cleanup(text: str, llm_model: str = DEFAULT_LLM_MODEL, llm_url: str = DEFAULT_LLM_URL, timeout_sec: int = 15) -> str:
    """Post-process transcription via local Ollama LLM for intelligent cleanup."""
    import urllib.request
    import urllib.error

    if not text or not text.strip():
        return text

    payload = json.dumps({
        "model": llm_model,
        "prompt": LLM_CLEANUP_PROMPT.format(text=text.strip()),
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 512},
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            llm_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            result = (body.get("response") or "").strip()
            if result:
                append_runtime_log(f"llm-cleanup model={llm_model} in={text!r} out={result!r}")
                return result
            append_runtime_log(f"llm-cleanup empty response, keeping original")
            return text.strip()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        append_runtime_log(f"llm-cleanup failed (falling back to gentle): {e}")
        return apply_gentle_rewrite(apply_soft_replacements(text))
    except Exception as e:
        append_runtime_log(f"llm-cleanup unexpected error (falling back to gentle): {e}")
        return apply_gentle_rewrite(apply_soft_replacements(text))


def normalize_transcript(text: str, cleanup_mode: str = DEFAULT_CLEANUP_MODE, llm_model: str = DEFAULT_LLM_MODEL, llm_url: str = DEFAULT_LLM_URL) -> str:
    cleaned = apply_soft_replacements(text)
    mode = str(cleanup_mode or "soft").strip().lower()
    if mode == "raw":
        return (text or "").strip()
    if mode == "llm":
        return llm_cleanup(cleaned, llm_model=llm_model, llm_url=llm_url)
    if mode == "gentle":
        return apply_gentle_rewrite(cleaned)
    return cleaned


# --- Fine-tuning data collector ---
FINETUNE_DIR = APP_DIR / "finetune-data"


class FinetuneCollector:
    """Saves audio + transcript pairs for future Whisper fine-tuning."""

    def __init__(self, enabled: bool = False, output_dir: Path = FINETUNE_DIR):
        self.enabled = enabled
        self.output_dir = output_dir
        self._manifest_path = output_dir / "manifest.jsonl"

    def save(self, wav_path: str, raw_text: str, cleaned_text: str, lang: str = "fr"):
        """Copy wav and append transcript entry to manifest."""
        if not self.enabled or not wav_path or not cleaned_text:
            return
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            import shutil
            ts = int(time.time() * 1000)
            dest_wav = self.output_dir / f"sample-{ts}.wav"
            shutil.copy2(wav_path, dest_wav)

            entry = {
                "audio": str(dest_wav.name),
                "raw": raw_text,
                "text": cleaned_text,
                "language": lang,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            with self._manifest_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

            append_runtime_log(f"finetune-save {dest_wav.name} text={cleaned_text!r}")
        except Exception as e:
            append_runtime_log(f"finetune-save-error: {e}")

    def stats(self) -> dict:
        """Return count and total size of collected samples."""
        if not self._manifest_path.exists():
            return {"samples": 0, "size_mb": 0.0}
        lines = self._manifest_path.read_text(encoding="utf-8").strip().splitlines()
        total_size = sum(f.stat().st_size for f in self.output_dir.glob("*.wav") if f.exists())
        return {"samples": len(lines), "size_mb": round(total_size / (1024 * 1024), 1)}


class Recorder:
    def __init__(self, sample_rate: int = 16000, channels: int = 1):
        self.sample_rate = sample_rate
        self.channels = channels
        self._chunks = []
        self._stream = None
        self._lock = threading.Lock()
        self.is_recording = False

    def _callback(self, indata, frames, time_info, status):
        if status:
            append_runtime_log(f"recorder-status: {status}")
        with self._lock:
            self._chunks.append(indata.copy())

    def start(self):
        with self._lock:
            if self.is_recording:
                return False
            self._chunks = []
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
                callback=self._callback,
                blocksize=1024,
            )
            self._stream.start()
            self.is_recording = True
            return True

    def stop_to_wav(self, out_dir: str) -> str:
        with self._lock:
            if not self.is_recording:
                raise RuntimeError("not recording")
            stream = self._stream
            self._stream = None
            self.is_recording = False
        try:
            if stream:
                stream.stop()
                stream.close()
        finally:
            pass

        with self._lock:
            chunks = self._chunks[:]
            self._chunks = []

        if not chunks:
            raise RuntimeError("no audio captured")

        audio = np.concatenate(chunks, axis=0).reshape(-1)
        audio = np.clip(audio, -1.0, 1.0)
        pcm16 = (audio * 32767.0).astype(np.int16)

        Path(out_dir).mkdir(parents=True, exist_ok=True)
        out_path = os.path.join(out_dir, f"tania-dictee-{int(time.time() * 1000)}.wav")
        with wave.open(out_path, "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(pcm16.tobytes())
        return out_path


class Transcriber:
    def __init__(self, model_name: str, language: str, device: str, compute_type: str, beam_size: int, initial_prompt: str):
        self.model_name = model_name
        self.language = None if language in ("", "auto", None) else language
        self.device = device
        self.compute_type = compute_type
        self.beam_size = max(1, int(beam_size))
        self.initial_prompt = initial_prompt or None
        self._model = None
        self._lock = threading.Lock()

    def _get_model(self):
        with self._lock:
            if self._model is None:
                append_runtime_log(f"stt-load model={self.model_name} device={self.device} compute={self.compute_type}")
                self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
            return self._model

    def transcribe(self, wav_path: str):
        model = self._get_model()
        started = time.time()
        segments, info = model.transcribe(
            wav_path,
            language=self.language,
            beam_size=self.beam_size,
            vad_filter=True,
            condition_on_previous_text=True,
            word_timestamps=False,
            temperature=0.0,
            initial_prompt=self.initial_prompt,
        )
        text_parts = []
        for seg in segments:
            t = (seg.text or "").strip()
            if t:
                text_parts.append(t)
        text = " ".join(text_parts).strip()
        return {
            "text": text,
            "elapsedMs": int((time.time() - started) * 1000),
            "language": getattr(info, "language", None),
            "prob": float(getattr(info, "language_probability", 0.0) or 0.0),
        }


class Paster:
    def __init__(self, method: str = "clipboard", restore_delay_ms: int = 450):
        self.method = method
        self.restore_delay_ms = max(150, int(restore_delay_ms))

    def paste(self, text: str):
        if not text:
            return
        if self.method == "typing":
            try:
                keyboard.write(text, delay=0.001)
            except Exception as typing_error:
                append_runtime_log(f"typing-fallback pyautogui because keyboard.write failed: {typing_error}")
                pyautogui.write(text, interval=0.001)
            return

        old_clip = None
        try:
            try:
                old_clip = pyperclip.paste()
            except Exception:
                old_clip = None

            pyperclip.copy(text)
            time.sleep(0.09)
            try:
                keyboard.release("ctrl")
                keyboard.release("shift")
                keyboard.release("alt")
            except Exception:
                pass
            keyboard.send("ctrl+v")
            time.sleep(self.restore_delay_ms / 1000.0)
        finally:
            if old_clip is not None:
                try:
                    pyperclip.copy(old_clip)
                except Exception:
                    pass


@dataclass
class AppConfig:
    model: str = "small"
    language: str = "fr"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 5
    sample_rate: int = 16000
    paste_method: str = "clipboard"
    clipboard_restore_delay_ms: int = 450
    no_paste: bool = False
    initial_prompt: str = DEFAULT_INITIAL_PROMPT
    glossary_path: str = str(DEFAULT_GLOSSARY_FILE)
    hotkey: str = DEFAULT_HOTKEY
    cancel_key: str = DEFAULT_CANCEL_KEY
    min_hold_ms: int = 180
    release_debounce_ms: int = 45
    send_enter: bool = False
    cleanup_mode: str = DEFAULT_CLEANUP_MODE
    llm_model: str = DEFAULT_LLM_MODEL
    llm_url: str = DEFAULT_LLM_URL
    collect_finetune: bool = False


class FeedbackOverlay:
    """Floating toast — runs on the MAIN thread via tkinter mainloop.
    Keyboard callbacks call set_state() from their thread; root.after(0,...) is
    the only tkinter-safe cross-thread call and schedules the update correctly."""

    _PULSE_COLORS = ["#CC2222", "#FF3333", "#FF5555", "#FF3333"]
    _SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self):
        import tkinter as tk
        self._root = tk.Tk()
        self._root.withdraw()

        win = tk.Toplevel(self._root)
        win.withdraw()
        win.overrideredirect(True)
        win.wm_attributes("-topmost", True)
        win.wm_attributes("-alpha", 0.93)
        win.configure(bg="#1A1A1A")
        self._win = win

        self._label = tk.Label(
            win,
            text="",
            font=("Segoe UI", 11, "bold"),
            fg="white",
            bg="#1A1A1A",
            padx=18,
            pady=10,
            anchor="w",
        )
        self._label.pack(fill="x")
        win.update_idletasks()

        self._state = "hidden"
        self._anim_job = None
        self._hide_job = None
        self._anim_idx = 0
        self._recording_start_ts: float = 0.0
        append_runtime_log("overlay-init ok")

    def set_state(self, state: str, message: str = ""):
        """Thread-safe: schedules state change on the tkinter main thread."""
        try:
            self._root.after(0, lambda s=state, m=message: self._apply(s, m))
        except Exception:
            pass

    def mainloop(self):
        """Replaces keyboard.wait() — runs the tkinter event loop on main thread."""
        self._root.mainloop()

    def _no_activate(self):
        """Apply WS_EX_NOACTIVATE so the window never steals keyboard focus (main thread only)."""
        try:
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            hwnd = self._win.winfo_id()
            cur = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, cur | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
            )
        except Exception:
            pass

    def _position(self):
        self._win.update_idletasks()
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        w = max(self._win.winfo_reqwidth(), 260)
        h = self._win.winfo_reqheight()
        self._win.geometry(f"{w}x{h}+{sw - w - 20}+{sh - h - 60}")

    def _cancel_jobs(self):
        for attr in ("_anim_job", "_hide_job"):
            job = getattr(self, attr)
            if job:
                try:
                    self._root.after_cancel(job)
                except Exception:
                    pass
            setattr(self, attr, None)

    def _show(self):
        self._win.deiconify()
        self._position()
        self._no_activate()

    def _apply(self, state: str, msg: str):
        self._state = state
        self._cancel_jobs()

        if state in ("hidden", "idle", "cancelled", "ignored", "empty"):
            self._win.withdraw()
            return

        if state == "recording":
            self._recording_start_ts = time.time()
            self._win.configure(bg="#1A0000")
            self._label.configure(bg="#1A0000", fg="#FF4444", text="🎙️  Enregistrement...")
            self._show()
            self._anim_idx = 0
            self._pulse()

        elif state == "processing":
            self._win.configure(bg="#1A1A2A")
            self._label.configure(bg="#1A1A2A", fg="#AAAACC", text="⏳  Traitement...")
            self._show()
            self._anim_idx = 0
            self._spin()

        elif state == "done":
            self._win.withdraw()

        elif state == "error":
            short = (msg[:72] + "…") if len(msg) > 72 else msg
            self._win.configure(bg="#1A0000")
            self._label.configure(bg="#1A0000", fg="#FF6666", text=f"❌  {short}")
            self._show()
            self._hide_job = self._root.after(5000, lambda: self._apply("hidden", ""))

    def _pulse(self):
        if self._state != "recording":
            return
        elapsed = int(time.time() - self._recording_start_ts) if self._recording_start_ts else 0
        if elapsed >= 45:
            # Clignotement rouge/orange à 45s — signal visuel, pas d'arrêt forcé
            color = "#FF2200" if (self._anim_idx % 2 == 0) else "#FF8800"
            timer_text = f"  ⏱ {elapsed}s ⚠"
        elif elapsed >= 35:
            color = "#FF8800"  # orange dès 35s
            timer_text = f"  ⏱ {elapsed}s"
        else:
            color = self._PULSE_COLORS[self._anim_idx % len(self._PULSE_COLORS)]
            timer_text = f"  {elapsed}s" if elapsed > 0 else ""
        self._label.configure(fg=color, text=f"\U0001f3a4  Enregistrement...{timer_text}")
        self._anim_idx += 1
        self._anim_job = self._root.after(250, self._pulse)

    def _spin(self):
        if self._state != "processing":
            return
        frame = self._SPINNER[self._anim_idx % len(self._SPINNER)]
        self._label.configure(text=f"{frame}  Traitement...")
        self._anim_idx += 1
        self._anim_job = self._root.after(100, self._spin)


class TaniaDicteeApp:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.recorder = Recorder(sample_rate=cfg.sample_rate, channels=1)
        glossary_terms = load_glossary_terms(cfg.glossary_path)
        prompt = build_initial_prompt(glossary_terms)
        append_runtime_log(f"glossary-terms count={len(glossary_terms)} path={cfg.glossary_path}")
        self.transcriber = Transcriber(
            model_name=cfg.model,
            language=cfg.language,
            device=cfg.device,
            compute_type=cfg.compute_type,
            beam_size=cfg.beam_size,
            initial_prompt=prompt if cfg.initial_prompt == DEFAULT_INITIAL_PROMPT else cfg.initial_prompt,
        )
        self.paster = Paster(method=cfg.paste_method, restore_delay_ms=cfg.clipboard_restore_delay_ms)
        self.finetune_collector = FinetuneCollector(enabled=cfg.collect_finetune)
        self.hotkey_keys, self.cancel_key = validate_hotkey_config(cfg.hotkey, cfg.cancel_key)
        self._state_lock = threading.Lock()
        self._transcribing = False
        self._cancel_requested = False
        self._last_start_ts = 0.0
        self._state = "idle"
        self._release_timer = None
        self.overlay = FeedbackOverlay()

    def _set_state(self, state: str, detail: str = ""):
        with self._state_lock:
            self._state = state
        stamp = time.strftime("%H:%M:%S")
        suffix = f" — {detail}" if detail else ""
        append_runtime_log(f"{state.upper()}{suffix}")
        write_runtime_state(state, detail)
        print(f"[{stamp}] {state.upper()}{suffix}")

    def _cancel_release_timer(self):
        timer = self._release_timer
        self._release_timer = None
        if timer:
            try:
                timer.cancel()
            except Exception:
                pass

    def start_recording(self):
        try:
            with self._state_lock:
                if self._transcribing:
                    self._set_state("busy", "still transcribing previous clip")
                    ping("busy")
                    return
                if self.recorder.is_recording:
                    return
                self._cancel_requested = False
                self._last_start_ts = time.time()
            self._cancel_release_timer()
            if self.recorder.start():
                self._set_state("recording", f"hold {self.cfg.hotkey} · cancel {self.cancel_key}")
                ping("recording")
                self.overlay.set_state("recording")
        except Exception as e:
            self._set_state("error", f"record start failed: {e}")
            ping("error")
            self.overlay.set_state("error", f"record start failed: {e}")

    def request_cancel(self):
        with self._state_lock:
            if self._transcribing:
                self._set_state("busy", "already transcribing — cancel ignored")
                ping("busy")
                return
            if not self.recorder.is_recording:
                return
            self._cancel_requested = True
        self._set_state("cancel", "dropping current clip")
        self.stop_recording()

    def _schedule_stop_if_hotkey_released(self):
        with self._state_lock:
            if self._transcribing or not self.recorder.is_recording:
                return
        self._cancel_release_timer()
        delay = max(0.0, float(self.cfg.release_debounce_ms) / 1000.0)
        timer = threading.Timer(delay, self._stop_if_hotkey_inactive)
        timer.daemon = True
        self._release_timer = timer
        timer.start()

    def _stop_if_hotkey_inactive(self):
        with self._state_lock:
            if self._transcribing or not self.recorder.is_recording:
                return
        try:
            if any(keyboard.is_pressed(key) for key in self.hotkey_keys):
                return
        except Exception:
            pass
        self.stop_recording()

    def stop_recording(self):
        with self._state_lock:
            if self._transcribing or not self.recorder.is_recording:
                return
            self._transcribing = True
        self._cancel_release_timer()
        threading.Thread(target=self._finish_pipeline, daemon=True).start()

    def _finish_pipeline(self):
        wav_path = None
        try:
            hold_ms = int((time.time() - self._last_start_ts) * 1000)
            cancel_requested = self._cancel_requested
            temp_dir = os.path.join(tempfile.gettempdir(), "tania-dictee")
            wav_path = self.recorder.stop_to_wav(temp_dir)

            if cancel_requested:
                self.overlay.set_state("hidden")
                self._set_state("cancelled", f"clip dropped ({hold_ms} ms)")
                ping("cancelled")
                return

            if hold_ms < max(80, int(self.cfg.min_hold_ms)):
                self.overlay.set_state("hidden")
                self._set_state("ignored", f"clip too short ({hold_ms} ms < {self.cfg.min_hold_ms} ms)")
                ping("ignored")
                return

            self.overlay.set_state("processing")
            self._set_state("transcribing", f"captured {hold_ms} ms")
            result = self.transcriber.transcribe(wav_path)
            raw_text = (result.get("text") or "").strip()
            text = normalize_transcript(raw_text, self.cfg.cleanup_mode, llm_model=self.cfg.llm_model, llm_url=self.cfg.llm_url)
            lang = result.get("language") or "?"
            append_runtime_log(
                f"stt elapsedMs={result.get('elapsedMs', 0)} lang={lang} cleanup={self.cfg.cleanup_mode} raw={raw_text!r} text={text!r}"
            )
            # Collect fine-tuning data before wav is deleted
            self.finetune_collector.save(wav_path, raw_text, text, lang=lang)
            if not text:
                self.overlay.set_state("hidden")
                self._set_state("empty", "nothing recognized")
                ping("ignored")
                return
            write_last_transcript(text)
            actions = []
            if not self.cfg.no_paste:
                self.paster.paste(text)
                if self.cfg.send_enter:
                    time.sleep(0.05)
                    keyboard.send("enter")
                actions.append("pasted + enter" if self.cfg.send_enter else "pasted")
            if not actions:
                actions.append("no-paste mode")
            self.overlay.set_state("done")
            self._set_state("done", " + ".join(actions))
            ping("done")
        except Exception as e:
            self.overlay.set_state("error", str(e))
            self._set_state("error", str(e))
            ping("error")
            append_runtime_log(traceback.format_exc())
            traceback.print_exc()
        finally:
            if wav_path and os.path.exists(wav_path):
                try:
                    os.remove(wav_path)
                except Exception:
                    pass
            with self._state_lock:
                self._transcribing = False
                self._cancel_requested = False

    def run_hotkey_loop(self):
        ensure_runtime_dir()
        print(f"\n{APP_NAME}")
        print("=" * len(APP_NAME))
        print(f"Hold {self.cfg.hotkey} to talk. Release to transcribe and paste.")
        print(f"Cancel current clip with {self.cancel_key}.")
        gpu_info = f"GPU={'CUDA' if HAS_CUDA else 'none'}"
        finetune_info = ""
        if self.cfg.collect_finetune:
            stats = self.finetune_collector.stats()
            finetune_info = f" | FineTune={stats['samples']} samples ({stats['size_mb']} MB)"
        llm_info = f" | LLM={self.cfg.llm_model}" if self.cfg.cleanup_mode == "llm" else ""
        print(
            f"Model={self.cfg.model} | {gpu_info} | Language={self.cfg.language} | Paste={self.cfg.paste_method} | Cleanup={self.cfg.cleanup_mode}{llm_info} | Glossary={self.cfg.glossary_path} | "
            f"SampleRate={self.cfg.sample_rate} | MinHold={self.cfg.min_hold_ms}ms | RestoreDelay={self.cfg.clipboard_restore_delay_ms}ms{finetune_info}"
        )
        print("Press Ctrl+C to quit.\n")
        append_runtime_log(
            f"startup model={self.cfg.model} language={self.cfg.language} hotkey={self.cfg.hotkey} cleanup={self.cfg.cleanup_mode} "
            f"minHoldMs={self.cfg.min_hold_ms} restoreDelayMs={self.cfg.clipboard_restore_delay_ms} glossaryPath={self.cfg.glossary_path}"
        )
        write_runtime_state("idle", "ready")

        keyboard.add_hotkey(self.cfg.hotkey, self.start_recording, suppress=True, trigger_on_release=False)
        for key in self.hotkey_keys:
            keyboard.on_release_key(key, lambda e, _key=key: self._schedule_stop_if_hotkey_released(), suppress=False)
        keyboard.on_press_key(self.cancel_key, lambda e: self.request_cancel(), suppress=False)

        try:
            self.overlay.mainloop()
        except KeyboardInterrupt:
            print("\nBye.")


def run_test_file(cfg: AppConfig, test_file: str):
    if not os.path.exists(test_file):
        raise FileNotFoundError(test_file)
    t = Transcriber(cfg.model, cfg.language, cfg.device, cfg.compute_type, cfg.beam_size, cfg.initial_prompt)
    result = t.transcribe(test_file)
    print(normalize_transcript(result.get("text") or "", cfg.cleanup_mode, llm_model=cfg.llm_model, llm_url=cfg.llm_url))


def parse_args():
    p = argparse.ArgumentParser(description="Local Windows push-to-talk dictation (FR-QC optimized)")
    _auto_device = auto_detect_device()
    _auto_model = auto_detect_model(_auto_device)
    _auto_compute = auto_detect_compute_type(_auto_device)
    p.add_argument("--model", default=os.getenv("TANIA_DICTEE_MODEL", _auto_model), help=f"Whisper model (auto={_auto_model})")
    p.add_argument("--language", default=os.getenv("TANIA_DICTEE_LANGUAGE", "fr"), help="fr|en|auto")
    p.add_argument("--device", default=os.getenv("TANIA_DICTEE_DEVICE", _auto_device), help=f"cpu|cuda (auto={_auto_device})")
    p.add_argument("--compute-type", default=os.getenv("TANIA_DICTEE_COMPUTE", _auto_compute), help=f"int8|float16 (auto={_auto_compute})")
    p.add_argument("--beam-size", type=int, default=int(os.getenv("TANIA_DICTEE_BEAM", "5")))
    p.add_argument("--sample-rate", type=int, default=int(os.getenv("TANIA_DICTEE_SAMPLE_RATE", "16000")))
    p.add_argument("--paste-method", choices=["clipboard", "typing"], default=os.getenv("TANIA_DICTEE_PASTE", "clipboard"))
    p.add_argument("--clipboard-restore-delay-ms", type=int, default=int(os.getenv("TANIA_DICTEE_CLIPBOARD_RESTORE_DELAY_MS", "450")))
    p.add_argument("--no-paste", action="store_true")
    p.add_argument("--initial-prompt", default=os.getenv("TANIA_DICTEE_INITIAL_PROMPT", DEFAULT_INITIAL_PROMPT))
    p.add_argument("--glossary-file", default=os.getenv("TANIA_DICTEE_GLOSSARY_FILE", str(DEFAULT_GLOSSARY_FILE)), help="Path to newline-separated hotwords / glossary")
    p.add_argument("--print-effective-prompt", action="store_true", help="Print merged prompt/glossary and exit")
    p.add_argument("--hotkey", default=os.getenv("TANIA_DICTEE_HOTKEY", DEFAULT_HOTKEY), help="Ex: f6 or ctrl+space")
    p.add_argument("--cancel-key", default=os.getenv("TANIA_DICTEE_CANCEL_KEY", DEFAULT_CANCEL_KEY))
    p.add_argument("--min-hold-ms", type=int, default=int(os.getenv("TANIA_DICTEE_MIN_HOLD_MS", "180")))
    p.add_argument("--release-debounce-ms", type=int, default=int(os.getenv("TANIA_DICTEE_RELEASE_DEBOUNCE_MS", "45")))
    p.add_argument("--send-enter", action="store_true")
    p.add_argument("--cleanup-mode", choices=["raw", "soft", "gentle", "llm"], default=os.getenv("TANIA_DICTEE_CLEANUP_MODE", DEFAULT_CLEANUP_MODE), help="Post-transcription cleanup: raw keeps whisper output, soft fixes hotwords, gentle smooths punctuation, llm uses local Ollama for intelligent cleanup")
    p.add_argument("--llm-model", default=os.getenv("TANIA_DICTEE_LLM_MODEL", DEFAULT_LLM_MODEL), help="Ollama model for LLM cleanup mode")
    p.add_argument("--llm-url", default=os.getenv("TANIA_DICTEE_LLM_URL", DEFAULT_LLM_URL), help="Ollama API endpoint")
    p.add_argument("--collect-finetune", action="store_true", default=bool(os.getenv("TANIA_DICTEE_COLLECT_FINETUNE", "")), help="Save audio+transcript pairs for future Whisper fine-tuning")
    p.add_argument("--test-file", default="", help="Transcribe a file once and exit")
    return p.parse_args()


def main():
    args = parse_args()
    normalized_hotkey_keys, normalized_cancel_key = validate_hotkey_config(args.hotkey, args.cancel_key)
    cfg = AppConfig(
        model=args.model,
        language=args.language,
        device=args.device,
        compute_type=args.compute_type,
        beam_size=args.beam_size,
        sample_rate=args.sample_rate,
        paste_method=args.paste_method,
        clipboard_restore_delay_ms=max(150, int(args.clipboard_restore_delay_ms)),
        no_paste=args.no_paste,
        initial_prompt=args.initial_prompt,
        glossary_path=args.glossary_file,
        hotkey="+".join(normalized_hotkey_keys),
        cancel_key=normalized_cancel_key,
        min_hold_ms=max(80, int(args.min_hold_ms)),
        release_debounce_ms=max(0, int(args.release_debounce_ms)),
        send_enter=bool(args.send_enter),
        cleanup_mode=str(args.cleanup_mode or DEFAULT_CLEANUP_MODE),
        llm_model=str(args.llm_model or DEFAULT_LLM_MODEL).strip(),
        llm_url=str(args.llm_url or DEFAULT_LLM_URL).strip(),
        collect_finetune=bool(args.collect_finetune),
    )

    if args.print_effective_prompt:
        print(build_initial_prompt(load_glossary_terms(args.glossary_file)) if args.initial_prompt == DEFAULT_INITIAL_PROMPT else args.initial_prompt)
        return 0

    if args.test_file:
        run_test_file(cfg, args.test_file)
        return 0

    if not acquire_single_instance():
        print(f"{APP_NAME} is already running. Reusing existing instance.")
        return 0

    app = TaniaDicteeApp(cfg)
    app.run_hotkey_loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
