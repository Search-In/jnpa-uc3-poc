"""anpr — ANPR + OCR inference service for the JNPA UC-III PoC.

Pipeline: YOLOv8 plate detector -> PaddleOCR (PP-OCRv4, Indian fine-tune) ->
post-processor (regex + state-code whitelist + character-confusion fixer).
"""

__version__ = "0.1.0"
