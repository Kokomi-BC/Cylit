"""提取层（规划 §13.1）：文本层 / 矢量层 / 位图层 / 页面渲染。

全部基于 PyMuPDF（``import pymupdf``）。本模块不依赖任何业务 Service，
保证 CLI 可以独立运行（规划 §6.1 parsing 层约束）。
"""

from backend.parsing.extract.vector_layer import Drawing, extract_drawings
from backend.parsing.extract.image_layer import ImageInfo, extract_images
from backend.parsing.extract.text_layer import extract_text_blocks, open_pdf
from backend.parsing.extract.render import (
    render_crop_png, render_page_png, render_pages, save_crop,
)

__all__ = [
    "extract_text_blocks", "open_pdf",
    "extract_drawings", "Drawing",
    "extract_images", "ImageInfo",
    "render_pages", "render_crop_png", "render_page_png", "save_crop",
]
