"""Face quality + liveness (anti-spoofing) gates for the identity service.

Two independent checks run on a captured frame before it is embedded/matched:

  * **Quality** (always available, no model) — detects exactly one face and checks
    sharpness (variance of Laplacian), brightness, and face size. A blurred, dark,
    faceless, or multi-face frame is rejected with a specific reason so the client
    can prompt a retake. Deterministic OpenCV math; fully testable.

  * **Liveness / anti-spoofing** — pluggable. When an ONNX model is mounted
    (IDENTITY_LIVENESS_MODEL, MiniFASNet-style 80x80) it is the authoritative
    gate. Without a model it degrades to a passive *advisory* texture score that
    is logged but does NOT hard-reject (so the demo never false-rejects a live
    user); the model is the production artefact, like the ArcFace weights.

All heavy deps (cv2, numpy, onnxruntime) are imported lazily so the slim/synthetic
install is unaffected.
"""
from __future__ import annotations

import os
from typing import Optional

# Lazily-initialised singletons.
_CASCADE = None
_LIVENESS_SESSION = "uninit"  # "uninit" | None (absent) | session

# Same dev/prod classification as the auth/mode layers (one source of truth).
_DEV_ENVS = {"development", "dev", "local", "test"}


def _is_production() -> bool:
    return os.environ.get("APP_ENV", "development").strip().lower() not in _DEV_ENVS


# --------------------------------------------------------------------------- config
def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def quality_enabled() -> bool:
    return os.environ.get("IDENTITY_QUALITY_CHECK", "true").strip().lower() in {"1", "true", "yes", "on"}


def liveness_enabled() -> bool:
    # Off unless explicitly turned on (and only meaningful with a model present).
    return os.environ.get("IDENTITY_LIVENESS", "false").strip().lower() in {"1", "true", "yes", "on"}


def liveness_model_loaded() -> bool:
    """True when the anti-spoof ONNX model is mounted AND loads successfully.

    The production startup gate and ``/healthz`` use this: with
    ``IDENTITY_LIVENESS=true`` a missing/unloadable model is FATAL (no spoof may
    ever pass un-checked — see rule 1)."""
    return _liveness_session() is not None


def _blur_min() -> float:
    return _f("IDENTITY_QUALITY_BLUR_MIN", 30.0)          # Laplacian variance


def _bright_min() -> float:
    return _f("IDENTITY_QUALITY_BRIGHTNESS_MIN", 40.0)


def _bright_max() -> float:
    return _f("IDENTITY_QUALITY_BRIGHTNESS_MAX", 225.0)


def _face_frac_min() -> float:
    return _f("IDENTITY_QUALITY_FACE_FRACTION_MIN", 0.10)  # face width / image width


def _liveness_threshold() -> float:
    return _f("IDENTITY_LIVENESS_THRESHOLD", 0.5)


def _liveness_real_index() -> int:
    # Output index that means "real/live" (hairymax AntiSpoofing_bin: index 0).
    try:
        return int(os.environ.get("IDENTITY_LIVENESS_REAL_INDEX", "0"))
    except ValueError:
        return 0


def _liveness_face_scale() -> float:
    # Face-bbox expansion before the model (the "_1.5_" in the model name).
    return _f("IDENTITY_LIVENESS_FACE_SCALE", 1.5)


def _liveness_size() -> int:
    try:
        return int(os.environ.get("IDENTITY_LIVENESS_SIZE", "128"))
    except ValueError:
        return 128


# --------------------------------------------------------------------------- helpers
def _cascade():
    global _CASCADE
    if _CASCADE is None:
        import cv2  # lazy

        _CASCADE = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
    return _CASCADE


def _detect_faces(gray):
    return _cascade().detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))


# --------------------------------------------------------------------------- quality
def assess_quality(image: Optional[bytes]) -> dict:
    """Assess a captured frame. Returns a dict:
        {ok, reason, faces, blur, brightness, face_fraction}
    ``ok`` is True only when exactly one sufficiently-large, sharp, well-lit face
    is present. ``reason`` is a machine code on failure (the client maps it to a
    retake hint). If decoding fails -> ok False, reason 'decode_failed'.
    """
    if not image:
        return {"ok": False, "reason": "no_image"}
    try:
        import cv2  # lazy
        import numpy as np  # lazy

        arr = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return {"ok": False, "reason": "decode_failed"}
        gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
        faces = _detect_faces(gray)
        n = len(faces)
        if n == 0:
            return {"ok": False, "reason": "no_face_detected", "faces": 0}
        if n > 1:
            return {"ok": False, "reason": "multiple_faces", "faces": int(n)}
        x, y, w, h = faces[0]
        face_fraction = round(float(w) / arr.shape[1], 4)
        face_gray = gray[max(0, y):y + h, max(0, x):x + w]
        blur = round(float(cv2.Laplacian(face_gray, cv2.CV_64F).var()), 2)
        brightness = round(float(face_gray.mean()), 2)
        metrics = {"faces": 1, "blur": blur, "brightness": brightness,
                   "face_fraction": face_fraction}
        if face_fraction < _face_frac_min():
            return {"ok": False, "reason": "face_too_small", **metrics}
        if blur < _blur_min():
            return {"ok": False, "reason": "image_too_blurry", **metrics}
        if brightness < _bright_min():
            return {"ok": False, "reason": "too_dark", **metrics}
        if brightness > _bright_max():
            return {"ok": False, "reason": "too_bright", **metrics}
        return {"ok": True, "reason": "ok", **metrics}
    except Exception as exc:  # noqa: BLE001 - never crash verification on QA
        # Production fails CLOSED: an unassessable frame is rejected (no degraded
        # state). Dev keeps the lenient advisory pass so the demo never blocks.
        return {"ok": not _is_production(), "reason": "quality_check_error", "error": str(exc)}


# --------------------------------------------------------------------------- liveness
def _liveness_session():
    """Load the anti-spoofing ONNX model once (None if not configured/available)."""
    global _LIVENESS_SESSION
    if _LIVENESS_SESSION == "uninit":
        path = os.environ.get("IDENTITY_LIVENESS_MODEL", "").strip()
        if not path or not os.path.isfile(path):
            _LIVENESS_SESSION = None
        else:
            try:
                import onnxruntime as ort  # lazy

                _LIVENESS_SESSION = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
            except Exception:  # noqa: BLE001
                _LIVENESS_SESSION = None
    return _LIVENESS_SESSION


def liveness_check(image: Optional[bytes]) -> dict:
    """Anti-spoofing. Returns {checked, live, score, reason, advisory?}.

    * With a model -> authoritative: ``checked=True``, ``live`` reflects the
      model's real-face probability vs IDENTITY_LIVENESS_THRESHOLD.
    * Without a model -> ``checked=False`` and a passive texture ``score`` is
      returned as ADVISORY only; ``live=True`` so it never hard-rejects a real
      user. Production must mount a model to make this authoritative.
    """
    if not image:
        return {"checked": False, "live": True, "score": None, "reason": "no_image"}
    try:
        import cv2  # lazy
        import numpy as np  # lazy

        arr = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return {"checked": False, "live": True, "score": None, "reason": "decode_failed"}

        session = _liveness_session()
        if session is not None:
            # Anti-spoof model (hairymax AntiSpoofing_bin): detect face, expand the
            # bbox by the model's scale, letterbox to NxN, BGR /255, NCHW, softmax.
            n = _liveness_size()
            gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
            faces = _detect_faces(gray)
            if len(faces):
                x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                scale = _liveness_face_scale()
                cx, cy = x + w / 2, y + h / 2
                side = int(max(w, h) * scale)
                x0, y0 = int(cx - side / 2), int(cy - side / 2)
                crop = arr[max(0, y0):y0 + side, max(0, x0):x0 + side]
            else:
                crop = arr
            # aspect-preserving resize + zero-pad to NxN
            oh, ow = crop.shape[:2]
            r = float(n) / max(oh, ow)
            rh, rw = int(oh * r), int(ow * r)
            resized = cv2.resize(crop, (max(1, rw), max(1, rh)))
            dh, dw = n - rh, n - rw
            t, b = dh // 2, dh - dh // 2
            l, rt = dw // 2, dw - dw // 2
            padded = cv2.copyMakeBorder(resized, t, b, l, rt, cv2.BORDER_CONSTANT, value=[0, 0, 0])
            blob = (padded.transpose(2, 0, 1).astype("float32") / 255.0)[None]
            out = session.run(None, {session.get_inputs()[0].name: blob})[0][0]
            e = np.exp(out - np.max(out)); probs = e / e.sum()
            idx = _liveness_real_index()
            real_p = float(probs[idx]) if idx < len(probs) else float(probs[0])
            ok = real_p >= _liveness_threshold()
            return {"checked": True, "live": ok, "score": round(real_p, 4),
                    "reason": "ok" if ok else "spoof_detected"}

        # No model -> passive advisory texture score (sharpness as a weak proxy).
        gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
        texture = round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 2)
        return {"checked": False, "live": True, "score": texture,
                "reason": "no_liveness_model", "advisory": True}
    except Exception as exc:  # noqa: BLE001
        return {"checked": False, "live": True, "score": None,
                "reason": "liveness_check_error", "error": str(exc)}


__all__ = ["quality_enabled", "liveness_enabled", "assess_quality", "liveness_check"]
