from __future__ import annotations

import logging
import re
from typing import Optional

from app.models import ScoreboardReading

try:
    import easyocr  # type: ignore
except ImportError:  # pragma: no cover - optional runtime dependency
    easyocr = None


_SCORE_RE = re.compile(r"(?P<home>\d{1,2})\s*[-:]\s*(?P<away>\d{1,2})")


class ScoreboardOCR:
    def __init__(self, *, roi: tuple[float, float, float, float], ocr_confidence_min: float, logger: Optional[logging.Logger] = None) -> None:
        self.roi = roi
        self.ocr_confidence_min = ocr_confidence_min
        self.logger = logger or logging.getLogger(__name__)
        self._easyocr_reader = easyocr.Reader(["en"], gpu=False) if easyocr is not None else None
        self.logger.info(
            "scoreboard OCR initialized",
            extra={
                "extra": {
                    "engine": "easyocr" if self._easyocr_reader is not None else "none",
                    "roi": list(roi),
                    "confidence_min": ocr_confidence_min,
                }
            },
        )

    def read(self, frame: object) -> ScoreboardReading:
        if self._easyocr_reader is None:
            self.logger.debug("scoreboard OCR skipped; no OCR engine available")
            return ScoreboardReading(None, None, None, None, 0.0)

        roi_frame = _crop_roi(frame, self.roi)
        try:
            results = self._easyocr_reader.readtext(roi_frame, detail=1, paragraph=False)
        except Exception as exc:  # pragma: no cover - defensive runtime path
            self.logger.warning("scoreboard OCR failed", extra={"extra": {"error": str(exc)}})
            return ScoreboardReading(None, None, None, None, 0.0)

        if not results:
            self.logger.debug("scoreboard OCR returned no text")
            return ScoreboardReading(None, None, None, None, 0.0)

        texts = [str(item[1]).strip() for item in results]
        confidence = max(float(item[2]) for item in results)
        self.logger.debug(
            "scoreboard OCR raw result",
            extra={"extra": {"texts": texts, "confidence": round(confidence, 4), "candidates": len(results)}},
        )

        if confidence < self.ocr_confidence_min:
            self.logger.debug(
                "scoreboard OCR rejected by confidence threshold",
                extra={"extra": {"confidence": round(confidence, 4), "required_min": self.ocr_confidence_min}},
            )
            return ScoreboardReading(None, None, None, None, confidence)

        home_score: Optional[int] = None
        away_score: Optional[int] = None
        for text in texts:
            match = _SCORE_RE.search(text)
            if match:
                home_score = int(match.group("home"))
                away_score = int(match.group("away"))
                break

        home_label, away_label = _guess_team_labels(texts)
        self.logger.debug(
            "scoreboard OCR parsed",
            extra={
                "extra": {
                    "home_label": home_label,
                    "away_label": away_label,
                    "home_score": home_score,
                    "away_score": away_score,
                }
            },
        )
        return ScoreboardReading(home_label, away_label, home_score, away_score, confidence)


def _guess_team_labels(texts: list[str]) -> tuple[Optional[str], Optional[str]]:
    cleaned = [re.sub(r"[^A-Za-z0-9 ]", "", text).strip() for text in texts]
    cleaned = [text for text in cleaned if text and not _SCORE_RE.search(text)]
    if len(cleaned) >= 2:
        return cleaned[0], cleaned[-1]
    if len(cleaned) == 1:
        return cleaned[0], None
    return None, None


def _crop_roi(frame: object, roi: tuple[float, float, float, float]) -> object:
    height = int(frame.shape[0])
    width = int(frame.shape[1])
    x0 = max(0, min(width - 1, int(width * roi[0])))
    y0 = max(0, min(height - 1, int(height * roi[1])))
    x1 = max(x0 + 1, min(width, int(width * (roi[0] + roi[2]))))
    y1 = max(y0 + 1, min(height, int(height * (roi[1] + roi[3]))))
    return frame[y0:y1, x0:x1]
