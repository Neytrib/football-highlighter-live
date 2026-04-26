from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

from app.models import TimerReading

try:
    import easyocr  # type: ignore
except ImportError:  # pragma: no cover - optional runtime dependency
    easyocr = None

try:
    import pytesseract  # type: ignore
except ImportError:  # pragma: no cover - optional runtime dependency
    pytesseract = None


_TIMER_MMSS_RE = re.compile(r"(?P<minute>\d{1,3})\s*[:.]\s*(?P<second>\d{1,2})")
_TIMER_PLUS_RE = re.compile(r"(?P<base>\d{1,3})\s*\+\s*(?P<injury>\d{1,2})(?:\s*[:.]\s*(?P<second>\d{1,2}))?")
_TIMER_MINUTE_RE = re.compile(r"^(?P<minute>\d{1,3})$")


def parse_timer_text(raw_text: str) -> Tuple[Optional[int], Optional[int]]:
    text = (raw_text or "").upper().strip()
    text = text.replace("O", "0")
    text = re.sub(r"[^0-9:+. ]", "", text)
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        return None, None

    match = _TIMER_MMSS_RE.search(text)
    if match:
        minute = int(match.group("minute"))
        second = int(match.group("second"))
        if 0 <= second <= 59:
            return minute, second
        return minute, 0

    match = _TIMER_PLUS_RE.search(text)
    if match:
        base = int(match.group("base"))
        injury = int(match.group("injury"))
        second_raw = match.group("second")
        second = int(second_raw) if second_raw else 0
        if second > 59:
            second = 0
        return base + injury, second

    match = _TIMER_MINUTE_RE.match(text)
    if match:
        return int(match.group("minute")), 0

    return None, None


class TimerOCR:
    def __init__(self, *, roi: tuple[float, float, float, float], ocr_confidence_min: float, logger: Optional[logging.Logger] = None) -> None:
        self.roi = roi
        self.ocr_confidence_min = ocr_confidence_min
        self.logger = logger or logging.getLogger(__name__)
        self._easyocr_reader = easyocr.Reader(["en"], gpu=False) if easyocr is not None else None
        engine = "easyocr" if self._easyocr_reader is not None else "pytesseract" if pytesseract is not None else "none"
        self.logger.info(
            "timer OCR initialized",
            extra={"extra": {"engine": engine, "roi": list(roi), "confidence_min": ocr_confidence_min}},
        )

    def read(self, frame: object) -> TimerReading:
        roi_frame = _crop_roi(frame, self.roi)

        text = ""
        confidence = 0.0

        if self._easyocr_reader is not None:
            try:
                results = self._easyocr_reader.readtext(roi_frame, detail=1, paragraph=False)
            except Exception as exc:  # pragma: no cover - defensive runtime path
                self.logger.warning("easyocr timer read failed", extra={"extra": {"error": str(exc)}})
                results = []
            if results:
                best = max(results, key=lambda item: float(item[2]))
                text = str(best[1])
                confidence = float(best[2])
                self.logger.debug(
                    "timer OCR easyocr result",
                    extra={"extra": {"raw_text": text, "confidence": round(confidence, 4), "candidates": len(results)}},
                )
        elif pytesseract is not None:
            try:
                text = str(pytesseract.image_to_string(roi_frame, config="--psm 7"))
                confidence = 0.5
                self.logger.debug(
                    "timer OCR tesseract result",
                    extra={"extra": {"raw_text": text.strip(), "confidence": confidence}},
                )
            except Exception as exc:  # pragma: no cover - defensive runtime path
                self.logger.warning("pytesseract timer read failed", extra={"extra": {"error": str(exc)}})

        minute, second = parse_timer_text(text)
        parse_ok = minute is not None and second is not None
        if confidence < self.ocr_confidence_min:
            minute, second = None, None
            self.logger.debug(
                "timer OCR rejected by confidence threshold",
                extra={
                    "extra": {
                        "raw_text": text.strip(),
                        "confidence": round(confidence, 4),
                        "required_min": self.ocr_confidence_min,
                        "parse_ok_before_reject": parse_ok,
                    }
                },
            )
        else:
            self.logger.debug(
                "timer OCR accepted",
                extra={
                    "extra": {
                        "raw_text": text.strip(),
                        "minute": minute,
                        "second": second,
                        "confidence": round(confidence, 4),
                    }
                },
            )

        return TimerReading(raw_text=text, minute=minute, second=second, confidence=confidence)


def _crop_roi(frame: object, roi: tuple[float, float, float, float]) -> object:
    # Works for ndarray-like frames without forcing numpy import here.
    height = int(frame.shape[0])
    width = int(frame.shape[1])
    x0 = max(0, min(width - 1, int(width * roi[0])))
    y0 = max(0, min(height - 1, int(height * roi[1])))
    x1 = max(x0 + 1, min(width, int(width * (roi[0] + roi[2]))))
    y1 = max(y0 + 1, min(height, int(height * (roi[1] + roi[3]))))
    return frame[y0:y1, x0:x1]
