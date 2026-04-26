from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - optional runtime dependency
    cv2 = None

try:
    from ultralytics import YOLO  # type: ignore
except ImportError:  # pragma: no cover - optional runtime dependency
    YOLO = None


class ClipCropper:
    def __init__(
        self,
        *,
        detector_model_path: str,
        detection_frame_stride: int,
        smoothing_alpha: float,
        target_class_names: tuple[str, ...],
        min_confidence: float,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.detector_model_path = detector_model_path
        self.detection_frame_stride = max(1, detection_frame_stride)
        self.smoothing_alpha = smoothing_alpha
        self.target_class_names = tuple(name.lower() for name in target_class_names)
        self.min_confidence = float(min_confidence)
        self.logger = logger or logging.getLogger(__name__)
        self._model = None

    def crop_clip(self, *, input_path: str, output_path: str, aspect_ratio: str = "1:1", enabled: bool = True) -> str:
        src = Path(input_path)
        dst = Path(output_path)
        dst.parent.mkdir(parents=True, exist_ok=True)

        if not enabled:
            shutil.copy2(src, dst)
            return str(dst)

        size = self._video_size(str(src))
        if size is None:
            shutil.copy2(src, dst)
            return str(dst)

        frame_w, frame_h = size
        crop_w, crop_h = _target_crop_size(frame_w, frame_h, aspect_ratio)
        center = self._detect_center(str(src), frame_w, frame_h)
        if center is None:
            center = (frame_w // 2, frame_h // 2)

        x = int(max(0, min(frame_w - crop_w, center[0] - crop_w // 2)))
        y = int(max(0, min(frame_h - crop_h, center[1] - crop_h // 2)))

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-vf",
            f"crop={crop_w}:{crop_h}:{x}:{y}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            str(dst),
        ]
        try:
            subprocess.run(cmd, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            self.logger.warning("crop failed, copying source clip", extra={"extra": {"error": str(exc)}})
            shutil.copy2(src, dst)

        return str(dst)

    def _load_model(self):
        if self._model is not None:
            return self._model
        if YOLO is None:
            return None
        model_path = Path(self.detector_model_path)
        if not model_path.exists():
            return None
        try:
            self._model = YOLO(str(model_path))
        except Exception as exc:  # pragma: no cover - runtime defensive
            self.logger.warning("failed to load YOLO model", extra={"extra": {"error": str(exc)}})
            self._model = None
        return self._model

    def _detect_center(self, video_path: str, frame_w: int, frame_h: int) -> Optional[Tuple[int, int]]:
        if cv2 is None:
            return None
        model = self._load_model()
        if model is None:
            return None
        class_ids = self._target_class_ids(model)

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return None

        idx = 0
        cx, cy = frame_w // 2, frame_h // 2
        found = False
        used_target_class = False

        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if idx % self.detection_frame_stride != 0:
                idx += 1
                continue

            try:
                results = model(frame, verbose=False)
            except Exception:  # pragma: no cover
                idx += 1
                continue

            boxes = getattr(results[0], "boxes", None) if results else None
            if boxes is None or boxes.xyxy is None or len(boxes.xyxy) == 0:
                idx += 1
                continue

            xyxy = boxes.xyxy.cpu().numpy()
            conf = boxes.conf.cpu().numpy() if boxes.conf is not None else None
            cls = boxes.cls.cpu().numpy().astype(int) if boxes.cls is not None else None
            best_idx = self._pick_detection_index(xyxy=xyxy, conf=conf, cls=cls, target_class_ids=class_ids)
            if best_idx is None:
                idx += 1
                continue
            box = xyxy[best_idx]
            bx = int((box[0] + box[2]) / 2)
            by = int((box[1] + box[3]) / 2)

            cx = int((1 - self.smoothing_alpha) * cx + self.smoothing_alpha * bx)
            cy = int((1 - self.smoothing_alpha) * cy + self.smoothing_alpha * by)
            found = True
            if cls is not None and class_ids and int(cls[best_idx]) in class_ids:
                used_target_class = True
            idx += 1

        cap.release()
        if found:
            self.logger.debug(
                "crop center detected from model",
                extra={
                    "extra": {
                        "video_path": video_path,
                        "center_x": cx,
                        "center_y": cy,
                        "used_target_class": used_target_class,
                        "target_class_names": list(self.target_class_names),
                    }
                },
            )
        else:
            self.logger.warning(
                "no model detections for crop center; fallback to frame center",
                extra={"extra": {"video_path": video_path}},
            )
        return (cx, cy) if found else None

    @staticmethod
    def _video_size(video_path: str) -> Optional[Tuple[int, int]]:
        if cv2 is None:
            return None
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return None
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        if width <= 0 or height <= 0:
            return None
        return width, height

    def _target_class_ids(self, model) -> set[int]:
        names = getattr(model, "names", {})
        target: set[int] = set()
        if isinstance(names, dict):
            for class_id, class_name in names.items():
                if str(class_name).lower() in self.target_class_names:
                    target.add(int(class_id))
        elif isinstance(names, list):
            for class_id, class_name in enumerate(names):
                if str(class_name).lower() in self.target_class_names:
                    target.add(int(class_id))
        return target

    def _pick_detection_index(
        self,
        *,
        xyxy,
        conf,
        cls,
        target_class_ids: set[int],
    ) -> Optional[int]:
        total = len(xyxy)
        if total == 0:
            return None

        confidences = conf if conf is not None else np.ones(total, dtype=float)
        classes = cls if cls is not None else np.full(total, -1, dtype=int)
        idx_all = np.arange(total)

        idx_conf = idx_all[confidences >= self.min_confidence]
        if len(idx_conf) == 0:
            return None

        if target_class_ids:
            idx_target = idx_conf[np.isin(classes[idx_conf], list(target_class_ids))]
            if len(idx_target) > 0:
                best = idx_target[int(confidences[idx_target].argmax())]
                return int(best)

        areas = (xyxy[idx_conf, 2] - xyxy[idx_conf, 0]) * (xyxy[idx_conf, 3] - xyxy[idx_conf, 1])
        scores = confidences[idx_conf] - (areas / max(1.0, areas.max())) * 0.1
        best = idx_conf[int(scores.argmax())]
        return int(best)


def _target_crop_size(width: int, height: int, aspect_ratio: str) -> tuple[int, int]:
    left_raw, right_raw = aspect_ratio.split(":", 1)
    left = float(left_raw)
    right = float(right_raw)
    target_ratio = left / right

    current_ratio = width / height
    if current_ratio > target_ratio:
        crop_h = height
        crop_w = int(crop_h * target_ratio)
    else:
        crop_w = width
        crop_h = int(crop_w / target_ratio)

    crop_w = max(2, min(width, crop_w))
    crop_h = max(2, min(height, crop_h))
    return crop_w, crop_h
