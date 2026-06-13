"""1D-convolutional trajectory autoencoder (UC-III Sub-Criterion 2C).

A small symmetric conv-AE over per-track trajectory features (speed series +
sin/cos heading; the dwell pattern is implicit in the speed channel — see
``features.py``). Trained on *normal* corridor trajectories to minimise
reconstruction MSE; at inference, a track whose reconstruction error exceeds the
99th-percentile training threshold is flagged ANOMALOUS_TRAJECTORY.

    encoder: Conv1d(F->16) -> ReLU -> Conv1d(16->32, stride 2) -> ReLU
             -> flatten -> Linear(-> latent)
    decoder: Linear(latent ->) -> ConvTranspose1d(32->16, stride 2) -> ReLU
             -> ConvTranspose1d(16->F) -> (seq_len, F)

torch is imported lazily and only inside the methods that need it, so this module
imports (and ``features``/``threshold`` work) on a host with no torch — the
service then runs rules-only and ``/train_ae`` reports torch is unavailable.
The trained checkpoint stores the state-dict, the config dims, and the
percentile threshold so inference is self-describing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List

import numpy as np

from ..config import AnomalyConfig
from .features import N_FEATURES


def _build_torch_module(cfg: AnomalyConfig):
    """Construct the torch ``nn.Module`` (lazy: only call when torch is present)."""
    import torch
    import torch.nn as nn

    seq_len = cfg.ae_seq_len
    # Conv with stride-2 halves the length once: reduced = ceil(seq_len/2).
    reduced = (seq_len + 1) // 2

    class TrajectoryAE(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.seq_len = seq_len
            self.reduced = reduced
            self.enc = nn.Sequential(
                nn.Conv1d(N_FEATURES, 16, kernel_size=3, padding=1),
                nn.ReLU(),
                nn.Conv1d(16, 32, kernel_size=3, stride=2, padding=1),
                nn.ReLU(),
            )
            self.to_latent = nn.Linear(32 * reduced, cfg.ae_latent)
            self.from_latent = nn.Linear(cfg.ae_latent, 32 * reduced)
            self.dec = nn.Sequential(
                nn.ConvTranspose1d(32, 16, kernel_size=3, stride=2,
                                   padding=1, output_padding=(seq_len % 2 == 0) and 1 or 0),
                nn.ReLU(),
                nn.ConvTranspose1d(16, N_FEATURES, kernel_size=3, padding=1),
            )

        def forward(self, x):  # x: (B, seq_len, F)
            z = x.transpose(1, 2)              # (B, F, seq_len)
            h = self.enc(z)                    # (B, 32, reduced)
            lat = self.to_latent(h.flatten(1))
            h2 = self.from_latent(lat).reshape(-1, 32, self.reduced)
            out = self.dec(h2)                 # (B, F, ~seq_len)
            out = out[..., : self.seq_len]     # crop any conv-transpose overshoot
            return out.transpose(1, 2)         # (B, seq_len, F)

    return TrajectoryAE()


def reconstruction_errors(recon: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Per-sample mean-squared reconstruction error. Both ``(N, seq_len, F)``."""
    diff = (recon - x) ** 2
    return diff.reshape(diff.shape[0], -1).mean(axis=1)


@dataclass
class AEResult:
    """Outcome of scoring one track against the trained AE."""

    error: float
    threshold: float
    is_anomalous: bool
    # error / threshold; >1 means above the gate. Useful for severity shading.
    ratio: float


class TrajectoryAutoencoder:
    """Train / load / score wrapper around the conv-AE.

    Holds the (lazily-built) torch module and the percentile threshold. ``score``
    works without retraining once ``load`` or ``train`` has populated the model.
    """

    def __init__(self, cfg: AnomalyConfig) -> None:
        self.cfg = cfg
        self.module = None
        self.threshold: float = float("inf")
        self.loaded = False

    # -- training -----------------------------------------------------------
    @staticmethod
    def torch_available() -> bool:
        try:
            import torch  # noqa: F401
            return True
        except Exception:  # noqa: BLE001
            return False

    def train(self, features: np.ndarray) -> dict:
        """Train the AE on normal-track features ``(N, seq_len, F)``.

        Returns a metrics dict (final loss, threshold, n_tracks). Sets the
        99th-percentile reconstruction error over the training set as the
        anomaly threshold.
        """
        import torch

        torch.manual_seed(self.cfg.ae_seed)
        np.random.seed(self.cfg.ae_seed)

        self.module = _build_torch_module(self.cfg)
        self.module.train()
        x = torch.as_tensor(features, dtype=torch.float32)
        opt = torch.optim.Adam(self.module.parameters(), lr=self.cfg.ae_lr)
        loss_fn = torch.nn.MSELoss()

        n = x.shape[0]
        batch = max(1, min(self.cfg.ae_batch, n))
        first_loss = last_loss = 0.0
        for epoch in range(self.cfg.ae_epochs):
            perm = torch.randperm(n)
            epoch_loss = 0.0
            for i in range(0, n, batch):
                idx = perm[i : i + batch]
                xb = x[idx]
                opt.zero_grad()
                recon = self.module(xb)
                loss = loss_fn(recon, xb)
                loss.backward()
                opt.step()
                epoch_loss += float(loss.detach()) * xb.shape[0]
            epoch_loss /= n
            if epoch == 0:
                first_loss = epoch_loss
            last_loss = epoch_loss

        # Threshold = configured percentile of training reconstruction errors.
        self.module.eval()
        with torch.no_grad():
            recon = self.module(x).cpu().numpy()
        errs = reconstruction_errors(recon, features)
        self.threshold = float(np.percentile(errs, self.cfg.ae_threshold_pct))
        self.loaded = True
        return {
            "n_tracks": int(n),
            "epochs": self.cfg.ae_epochs,
            "first_loss": round(first_loss, 6),
            "final_loss": round(last_loss, 6),
            "threshold_pct": self.cfg.ae_threshold_pct,
            "threshold": round(self.threshold, 6),
            "train_error_mean": round(float(errs.mean()), 6),
            "train_error_max": round(float(errs.max()), 6),
        }

    # -- persistence --------------------------------------------------------
    def save(self) -> None:
        import torch

        if self.module is None:
            raise RuntimeError("nothing to save: model not trained")
        Path(self.cfg.weights_dir).mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                "state_dict": self.module.state_dict(),
                "seq_len": self.cfg.ae_seq_len,
                "latent": self.cfg.ae_latent,
                "n_features": N_FEATURES,
                "threshold": self.threshold,
                "threshold_pct": self.cfg.ae_threshold_pct,
            },
            self.cfg.weights_path,
        )

    def load(self) -> bool:
        """Load weights + threshold from disk. Returns True on success."""
        path = Path(self.cfg.weights_path)
        if not path.is_file() or not self.torch_available():
            return False
        import torch

        ckpt = torch.load(str(path), map_location="cpu", weights_only=False)
        self.module = _build_torch_module(self.cfg)
        self.module.load_state_dict(ckpt["state_dict"])
        self.module.eval()
        self.threshold = float(ckpt.get("threshold", float("inf")))
        self.loaded = True
        return True

    # -- scoring ------------------------------------------------------------
    def score_batch(self, features: np.ndarray) -> List[AEResult]:
        """Score a batch of feature matrices ``(N, seq_len, F)``."""
        if not self.loaded or self.module is None:
            return []
        import torch

        self.module.eval()
        with torch.no_grad():
            recon = self.module(torch.as_tensor(features, dtype=torch.float32)).cpu().numpy()
        errs = reconstruction_errors(recon, features)
        out: List[AEResult] = []
        for e in errs:
            ratio = float(e) / self.threshold if self.threshold not in (0.0, float("inf")) else 0.0
            out.append(AEResult(
                error=float(e),
                threshold=self.threshold,
                is_anomalous=float(e) > self.threshold,
                ratio=round(ratio, 3),
            ))
        return out


def write_metrics(cfg: AnomalyConfig, metrics: dict) -> None:
    """Persist the AE training metrics summary next to the weights."""
    Path(cfg.weights_dir).mkdir(parents=True, exist_ok=True)
    Path(cfg.metrics_path).write_text(json.dumps(metrics, indent=2))


__all__ = [
    "TrajectoryAutoencoder",
    "AEResult",
    "reconstruction_errors",
    "write_metrics",
]
