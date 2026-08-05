"""Gate Document module (UC-III): EIR / PIN ticket / Form-13.

The three client gate documents that anchor the truck & gate lifecycle, with the
standard Data-Upload triad (template / validate / upload + history) and the
document-derived TAT that the corpus provides as ground truth.
"""
from .repository import GateDocumentRepository
from .service import GateDocumentService

__all__ = ["GateDocumentRepository", "GateDocumentService"]
