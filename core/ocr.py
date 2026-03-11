"""
PaddleOCR-based text extraction engine with spatial bounding boxes.
Runs as a dedicated OCR pipeline alongside VLM captioning for precise text extraction.
Supports multi-language, table detection, and layout-aware extraction.
"""
import io
import time
import logging
from typing import Optional, List, Tuple
from dataclasses import dataclass, field
from PIL import Image
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class OCRRegion:
    """A detected text region with spatial position."""
    text: str
    confidence: float
    bbox: Tuple[int, int, int, int]  # (x1, y1, x2, y2)
    polygon: Optional[List[Tuple[int, int]]] = None


@dataclass
class OCRResult:
    """Complete OCR result from a frame."""
    regions: List[OCRRegion] = field(default_factory=list)
    full_text: str = ""
    extraction_ms: float = 0.0
    engine: str = "paddleocr"

    @property
    def region_count(self) -> int:
        return len(self.regions)

    def text_in_area(self, x1: int, y1: int, x2: int, y2: int) -> List[OCRRegion]:
        """Find text regions that overlap with a given area."""
        results = []
        for r in self.regions:
            rx1, ry1, rx2, ry2 = r.bbox
            # Check overlap
            if rx1 < x2 and rx2 > x1 and ry1 < y2 and ry2 > y1:
                results.append(r)
        return results

    def to_spatial_json(self) -> list:
        """Export regions with spatial data for database storage."""
        return [
            {
                "text": r.text,
                "confidence": round(r.confidence, 3),
                "bbox": list(r.bbox),
            }
            for r in self.regions
        ]


class OCREngine:
    """
    PaddleOCR-based text extraction engine.
    Three-stage pipeline: text detection -> orientation classification -> recognition.
    Falls back to basic pytesseract if PaddleOCR unavailable.
    """

    def __init__(self, lang: str = "en", use_gpu: bool = False, use_angle_cls: bool = True):
        self.lang = lang
        self._rapid_ocr = None
        self._paddle_ocr = None
        self._tesseract_available = False
        self._engine_name = "none"

        # Try RapidOCR first (ONNX-based, most reliable)
        try:
            from rapidocr_onnxruntime import RapidOCR
            self._rapid_ocr = RapidOCR()
            self._engine_name = "rapidocr"
            logger.info("RapidOCR initialized (ONNX backend)")
        except Exception as e:
            logger.debug("RapidOCR not available: %s", e)

        # Try PaddleOCR as second option
        if not self._rapid_ocr:
            try:
                import os
                os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
                from paddleocr import PaddleOCR
                self._paddle_ocr = PaddleOCR(lang=lang)
                self._engine_name = "paddleocr"
                logger.info("PaddleOCR initialized (lang=%s)", lang)
            except Exception as e:
                logger.debug("PaddleOCR not available: %s", e)

        # Try Tesseract fallback
        if not self._rapid_ocr and not self._paddle_ocr:
            try:
                import pytesseract
                pytesseract.get_tesseract_version()
                self._tesseract_available = True
                self._engine_name = "tesseract"
                logger.info("Using Tesseract OCR fallback")
            except Exception as e:
                logger.warning("Tesseract not available: %s", e)  # signed: gamma

        if not self.is_available:
            logger.warning("No OCR engine available (install rapidocr-onnxruntime)")  # signed: gamma

    @property
    def is_available(self) -> bool:
        return self._rapid_ocr is not None or self._paddle_ocr is not None or self._tesseract_available

    def extract(self, image: Image.Image, min_confidence: float = 0.5) -> OCRResult:
        """
        Extract text from an image with spatial bounding boxes.

        Args:
            image: PIL Image (screenshot)
            min_confidence: Minimum confidence threshold for text regions

        Returns:
            OCRResult with spatial text regions and full extracted text
        """
        start = time.perf_counter()

        if self._rapid_ocr:
            result = self._extract_rapid(image, min_confidence)
        elif self._paddle_ocr:
            result = self._extract_paddle(image, min_confidence)
        elif self._tesseract_available:
            result = self._extract_tesseract(image, min_confidence)
        else:
            result = OCRResult()

        result.extraction_ms = (time.perf_counter() - start) * 1000
        result.engine = self._engine_name
        return result

    def _extract_rapid(self, image: Image.Image, min_confidence: float) -> OCRResult:
        """Extract text using RapidOCR (ONNX backend, PaddleOCR models)."""
        img_array = np.array(image)

        raw_result, elapse = self._rapid_ocr(img_array)

        regions = []
        texts = []

        if raw_result:
            for bbox_points, text, confidence in raw_result:
                conf = float(confidence) if confidence else 0.0
                if conf < min_confidence:
                    continue

                # bbox_points is [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
                xs = [p[0] for p in bbox_points]
                ys = [p[1] for p in bbox_points]
                bbox = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
                poly_tuples = [(int(p[0]), int(p[1])) for p in bbox_points]

                regions.append(OCRRegion(
                    text=str(text),
                    confidence=conf,
                    bbox=bbox,
                    polygon=poly_tuples,
                ))
                texts.append(str(text))

        # Sort top-to-bottom, left-to-right
        regions.sort(key=lambda r: (r.bbox[1] // 20, r.bbox[0]))
        full_text = "\n".join(texts)
        return OCRResult(regions=regions, full_text=full_text)

    def _extract_paddle(self, image: Image.Image, min_confidence: float) -> OCRResult:
        """Extract text using PaddleOCR's 3-stage pipeline."""
        img_array = np.array(image)

        # PaddleOCR expects BGR format
        if len(img_array.shape) == 3 and img_array.shape[2] == 3:
            img_array = img_array[:, :, ::-1]  # RGB -> BGR

        raw_result = self._paddle_ocr.ocr(img_array, cls=True)

        regions = []
        texts = []

        if raw_result and raw_result[0]:
            for line in raw_result[0]:
                polygon = line[0]  # [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
                text = line[1][0]
                confidence = line[1][1]

                if confidence < min_confidence:
                    continue

                # Convert polygon to bounding box
                xs = [p[0] for p in polygon]
                ys = [p[1] for p in polygon]
                bbox = (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))

                poly_tuples = [(int(p[0]), int(p[1])) for p in polygon]

                regions.append(OCRRegion(
                    text=text,
                    confidence=confidence,
                    bbox=bbox,
                    polygon=poly_tuples,
                ))
                texts.append(text)

        # Sort regions top-to-bottom, left-to-right for natural reading order
        regions.sort(key=lambda r: (r.bbox[1] // 20, r.bbox[0]))

        full_text = "\n".join(texts)
        return OCRResult(regions=regions, full_text=full_text)

    def _extract_tesseract(self, image: Image.Image, min_confidence: float) -> OCRResult:
        """Fallback extraction using Tesseract."""
        import pytesseract

        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
        regions = []
        texts = []

        for i in range(len(data["text"])):
            text = data["text"][i].strip()
            conf = float(data["conf"][i])

            if not text or conf < min_confidence * 100:  # Tesseract uses 0-100
                continue

            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            bbox = (x, y, x + w, y + h)

            regions.append(OCRRegion(
                text=text,
                confidence=conf / 100.0,
                bbox=bbox,
            ))
            texts.append(text)

        regions.sort(key=lambda r: (r.bbox[1] // 20, r.bbox[0]))
        full_text = " ".join(texts)
        return OCRResult(regions=regions, full_text=full_text)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from PIL import ImageGrab

    ocr = OCREngine()
    print(f"Engine: {ocr._engine_name}, Available: {ocr.is_available}")

    if ocr.is_available:
        img = ImageGrab.grab()
        print(f"Capturing screen ({img.width}x{img.height})...")
        result = ocr.extract(img)
        print(f"Found {result.region_count} text regions in {result.extraction_ms:.0f}ms")
        print(f"Full text ({len(result.full_text)} chars):")
        print(result.full_text[:500])
        print(f"\nFirst 5 regions:")
        for r in result.regions[:5]:
            print(f"  [{r.bbox}] ({r.confidence:.2f}) {r.text[:60]}")
