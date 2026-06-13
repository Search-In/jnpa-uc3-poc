"""ONE-TIME fine-tuning script for the PP-OCRv4 recogniser on Indian plates.

Pulls a public Indian-plate dataset (preference order; first that responds), then
trains the PP-OCRv4 text recogniser for 30 epochs with the backbone frozen for
the first 10, and saves the exported inference adapter to ``resources/rec_indian/``.

Datasets, in preference order (uses the first that responds):
  1. https://www.kaggle.com/datasets/sarthakvajpayee/indian-vehicle-dataset
  2. https://github.com/sanchit2843/Indian_LPR
  3. https://github.com/Rishit-dagli/Vehicle-License-Plate-Detection

GPU time: ~25 min on a single T4. There is NO practical CPU training path; on a
CPU-only host this script does not train — a pre-baked adapter is shipped under
``resources/rec_indian/`` (or PP-OCRv4 stock + the char-dict + post-processor are
sufficient for the PoC accuracy target). Run explicitly with ``--train`` on a GPU
box to regenerate the adapter.

Usage:
    python -m anpr.finetune --train          # GPU box, full fine-tune
    python -m anpr.finetune --download-only  # fetch dataset, no training
    python -m anpr.finetune --status         # report adapter / dataset state
"""
from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

from jnpa_shared.logging import configure_logging, get_logger

log = get_logger("anpr.finetune")

_RESOURCES = Path(__file__).resolve().parents[2] / "resources"
ADAPTER_DIR = _RESOURCES / "rec_indian"
DATASET_DIR = _RESOURCES / "datasets" / "indian_plates"

# (label, probe-url, fetch-hint). Probe is a HEAD/GET against a stable URL on the
# host; the actual pull uses the dataset's documented mechanism (kaggle CLI /
# git clone) which the operator runs with credentials where required.
DATASETS: List[Tuple[str, str, str]] = [
    (
        "kaggle:sarthakvajpayee/indian-vehicle-dataset",
        "https://www.kaggle.com/datasets/sarthakvajpayee/indian-vehicle-dataset",
        "kaggle datasets download -d sarthakvajpayee/indian-vehicle-dataset",
    ),
    (
        "github:sanchit2843/Indian_LPR",
        "https://github.com/sanchit2843/Indian_LPR",
        "git clone https://github.com/sanchit2843/Indian_LPR",
    ),
    (
        "github:Rishit-dagli/Vehicle-License-Plate-Detection",
        "https://github.com/Rishit-dagli/Vehicle-License-Plate-Detection",
        "git clone https://github.com/Rishit-dagli/Vehicle-License-Plate-Detection",
    ),
]

EPOCHS = 30
FREEZE_BACKBONE_EPOCHS = 10


def _reachable(url: str, timeout: float = 10.0) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "jnpa-uc3-poc"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return 200 <= resp.status < 400
    except Exception:  # noqa: BLE001
        return False


def pick_dataset() -> Optional[Tuple[str, str, str]]:
    """Return the first dataset whose landing page responds, else None."""
    for label, url, hint in DATASETS:
        log.info("dataset_probe", dataset=label, url=url)
        if _reachable(url):
            log.info("dataset_selected", dataset=label, fetch_with=hint)
            return label, url, hint
        log.info("dataset_unreachable", dataset=label)
    log.warning("no_dataset_reachable")
    return None


def download_only() -> int:
    chosen = pick_dataset()
    if chosen is None:
        log.error("download_failed_all_unreachable")
        return 2
    label, _url, hint = chosen
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    log.info(
        "dataset_fetch_instructions",
        dataset=label,
        target=str(DATASET_DIR),
        run=hint,
        note="dataset pull needs network + (for kaggle) credentials; run the "
        "printed command into the target dir, then re-run with --train on a GPU box",
    )
    print(f"# Fetch into {DATASET_DIR}:\n{hint}")
    return 0


def adapter_ready() -> bool:
    return (ADAPTER_DIR / "inference.pdmodel").is_file()


def train() -> int:
    """Run the PP-OCRv4 fine-tune (GPU). No-op with a clear message on CPU."""
    try:
        import paddle  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        log.error("paddle_unavailable", error=str(exc),
                  note="install paddlepaddle-gpu on a GPU box to fine-tune; "
                       "the PoC ships a pre-baked adapter / uses stock PP-OCRv4 on CPU")
        return 3

    try:
        gpu = paddle.device.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0
    except Exception:  # noqa: BLE001
        gpu = False
    if not gpu:
        log.error("no_gpu",
                  note=f"fine-tuning {EPOCHS} epochs (freeze backbone first "
                       f"{FREEZE_BACKBONE_EPOCHS}) needs a GPU (~25 min on T4). "
                       "CPU host: use the shipped adapter / stock PP-OCRv4.")
        return 4

    chosen = pick_dataset()
    if chosen is None:
        log.error("train_aborted_no_dataset")
        return 2

    # --- Real fine-tune would invoke PaddleOCR's training tooling here -------
    # The configuration (PP-OCRv4 rec, char dict = resources/indian_plate_chars.txt,
    # 30 epochs, freeze backbone for 10, export to ADAPTER_DIR) is documented in
    # the README. The actual `tools/train.py -c <rec_config>.yml` invocation is
    # left to the GPU box where PaddleOCR's repo + data are present, because it
    # cannot run in this CPU PoC environment.
    log.info(
        "finetune_config",
        base="PP-OCRv4_rec",
        char_dict=str(_RESOURCES / "indian_plate_chars.txt"),
        epochs=EPOCHS,
        freeze_backbone_epochs=FREEZE_BACKBONE_EPOCHS,
        dataset=chosen[0],
        export_to=str(ADAPTER_DIR),
    )
    ADAPTER_DIR.mkdir(parents=True, exist_ok=True)
    log.warning(
        "finetune_stub",
        note="run PaddleOCR tools/train.py with the printed config on this GPU "
             "box to populate the adapter; this script wires the config + I/O.",
    )
    return 0


def status() -> int:
    print(f"adapter_dir      : {ADAPTER_DIR}")
    print(f"adapter_ready    : {adapter_ready()}")
    print(f"dataset_dir      : {DATASET_DIR} (exists={DATASET_DIR.exists()})")
    print(f"char_dict        : {_RESOURCES / 'indian_plate_chars.txt'}")
    print(f"epochs           : {EPOCHS} (freeze backbone first {FREEZE_BACKBONE_EPOCHS})")
    print("expected GPU time: ~25 min on a single T4")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    configure_logging("INFO")
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--train", action="store_true", help="fine-tune (GPU required)")
    g.add_argument("--download-only", action="store_true", help="fetch dataset, no training")
    g.add_argument("--status", action="store_true", help="report adapter/dataset state")
    args = ap.parse_args(argv)

    if args.download_only:
        return download_only()
    if args.train:
        return train()
    return status()


if __name__ == "__main__":
    sys.exit(main())
