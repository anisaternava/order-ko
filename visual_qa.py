"""Conservative visual comparison of rendered kitchen plans."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageOps


def compare_plan_images(before: bytes | Path, after: bytes | Path, pixel_threshold: int = 28) -> dict:
    """Return a normalized pixel difference; this cannot identify appliance semantics."""
    def open_image(source: bytes | Path) -> Image.Image:
        data = BytesIO(source) if isinstance(source, bytes) else source
        with Image.open(data) as image:
            return image.convert("RGB").copy()

    first = open_image(before)
    second = open_image(after)
    exact = first.size == second.size and ImageChops.difference(first, second).getbbox() is None
    original_sizes = (first.size, second.size)
    if first.size != second.size:
        second = ImageOps.fit(second, first.size, method=Image.Resampling.LANCZOS)
    first = first.convert("L").filter(ImageFilter.GaussianBlur(0.6))
    second = second.convert("L").filter(ImageFilter.GaussianBlur(0.6))
    diff = ImageChops.difference(first, second)
    mask = diff.point(lambda value: 255 if value >= pixel_threshold else 0)
    histogram = mask.histogram()
    changed = histogram[255]
    total = first.width * first.height
    return {"changed_fraction": changed / total if total else 0.0, "bbox": mask.getbbox(),
            "size": first.size, "original_sizes": original_sizes, "exact_match": exact}


def render_attachment(path: Path) -> bytes:
    """Render the first page of a PDF, or normalize an image, as PNG bytes."""
    if path.suffix.lower() == ".pdf":
        import fitz
        with fitz.open(path) as document:
            if not document.page_count:
                raise ValueError("PDF has no pages")
            return document[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).tobytes("png")
    with Image.open(path) as image:
        output = BytesIO()
        image.convert("RGB").save(output, format="PNG")
        return output.getvalue()
