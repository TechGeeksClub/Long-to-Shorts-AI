from __future__ import annotations

import urllib.request
from pathlib import Path

import numpy as np

from app.models import CropKeyframe


YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
)


def even(value: float) -> int:
    integer = max(2, int(value))
    return integer if integer % 2 == 0 else integer - 1


def even_coordinate(value: float) -> int:
    integer = max(0, int(value))
    return integer if integer % 2 == 0 else integer - 1


def crop_dimensions(width: int, height: int) -> tuple[int, int]:
    target_ratio = 9 / 16
    if width / height > target_ratio:
        return even(height * target_ratio), even(height)
    return even(width), even(width / target_ratio)


def center_crop(
    source_width: int,
    source_height: int,
) -> tuple[list[CropKeyframe], int, int]:
    crop_width, crop_height = crop_dimensions(source_width, source_height)
    center_x = (source_width - crop_width) / 2
    center_y = (source_height - crop_height) / 2
    return [
        CropKeyframe(
            time=0.0,
            x=even_coordinate(center_x),
            y=even_coordinate(center_y),
            width=crop_width,
            height=crop_height,
        )
    ], crop_width, crop_height


def smooth_positions(values: list[float], alpha: float = 0.25) -> list[float]:
    if not values:
        return []
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def ensure_yunet_model(models_dir: Path) -> Path | None:
    destination = models_dir / "face_detection_yunet_2023mar.onnx"
    if destination.exists() and destination.stat().st_size > 100_000:
        return destination
    try:
        models_dir.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".download")
        urllib.request.urlretrieve(YUNET_URL, temporary)
        temporary.replace(destination)
        return destination
    except Exception:
        return None


def track_face_crop(
    *,
    source: Path,
    clip_start: float,
    clip_end: float,
    source_width: int,
    source_height: int,
    models_dir: Path,
    samples_per_second: float = 5.0,
) -> tuple[list[CropKeyframe], int, int]:
    crop_width, crop_height = crop_dimensions(source_width, source_height)
    center_x = max(0.0, (source_width - crop_width) / 2)
    center_y = max(0.0, (source_height - crop_height) / 2)
    model_path = ensure_yunet_model(models_dir)
    if model_path is None:
        return center_crop(source_width, source_height)

    try:
        import cv2

        capture = cv2.VideoCapture(str(source))
        if not capture.isOpened():
            raise RuntimeError("Video açılamadı")
        detector = cv2.FaceDetectorYN.create(str(model_path), "", (320, 320), 0.72, 0.3, 5000)
        times: list[float] = []
        positions_x: list[float] = []
        positions_y: list[float] = []
        previous_histogram: np.ndarray | None = None
        previous_x = center_x
        previous_y = center_y
        step = 1 / samples_per_second
        timestamp = clip_start
        while timestamp < clip_end:
            capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
            ok, frame = capture.read()
            if not ok:
                timestamp += step
                continue
            frame_height, frame_width = frame.shape[:2]
            scale = min(1.0, 640 / max(frame_width, frame_height))
            resized = cv2.resize(frame, None, fx=scale, fy=scale)
            resized_height, resized_width = resized.shape[:2]
            detector.setInputSize((resized_width, resized_height))

            gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
            histogram = cv2.calcHist([gray], [0], None, [32], [0, 256])
            cv2.normalize(histogram, histogram)
            scene_changed = (
                previous_histogram is not None
                and cv2.compareHist(previous_histogram, histogram, cv2.HISTCMP_BHATTACHARYYA)
                > 0.48
            )
            previous_histogram = histogram

            _, faces = detector.detect(resized)
            target_x = center_x
            target_y = center_y
            if faces is not None and len(faces):
                face = max(faces, key=lambda item: item[2] * item[3])
                face_center_x = (float(face[0]) + float(face[2]) / 2) / scale
                face_center_y = (float(face[1]) + float(face[3]) / 2) / scale
                target_x = min(max(0.0, face_center_x - crop_width / 2), source_width - crop_width)
                target_y = min(max(0.0, face_center_y - crop_height * 0.38), source_height - crop_height)
            if scene_changed:
                previous_x, previous_y = target_x, target_y
            else:
                previous_x = 0.25 * target_x + 0.75 * previous_x
                previous_y = 0.25 * target_y + 0.75 * previous_y
            times.append(timestamp - clip_start)
            positions_x.append(previous_x)
            positions_y.append(previous_y)
            timestamp += step
        capture.release()
    except Exception:
        return center_crop(source_width, source_height)

    if not times:
        times = [0.0]
        positions_x = [center_x]
        positions_y = [center_y]
    smoothed_x = smooth_positions(positions_x)
    smoothed_y = smooth_positions(positions_y)
    keyframes = [
        CropKeyframe(
            time=round(time, 3),
            x=even_coordinate(min(max(0.0, x), source_width - crop_width)),
            y=even_coordinate(min(max(0.0, y), source_height - crop_height)),
            width=crop_width,
            height=crop_height,
        )
        for time, x, y in zip(times, smoothed_x, smoothed_y, strict=True)
    ]
    return keyframes, crop_width, crop_height


def write_crop_commands(keyframes: list[CropKeyframe], destination: Path) -> None:
    lines = [
        f"{frame.time:.3f} cropper x {frame.x}, cropper y {frame.y};"
        for frame in keyframes
    ]
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
