"""Ингесторы: загрузка и первичный парсинг документов."""

from .base import Ingestor, IngestResult, SourceType
from .pdf import PDFIngestor
from .url import URLIngestor
from .excel import ExcelIngestor
from .image import ImageIngestor
from .text import TextIngestor
from .factory import get_ingestor, supported_extensions

__all__ = [
    "Ingestor", "IngestResult", "SourceType",
    "PDFIngestor", "URLIngestor", "ExcelIngestor", "ImageIngestor", "TextIngestor",
    "get_ingestor", "supported_extensions",
]
