from app.face_tracking import center_crop, crop_dimensions, smooth_positions


def test_landscape_crop_is_vertical_and_even() -> None:
    width, height = crop_dimensions(1920, 1080)
    assert width % 2 == 0
    assert height % 2 == 0
    assert abs(width / height - 9 / 16) < 0.01


def test_smoothing_limits_abrupt_movement() -> None:
    values = smooth_positions([0, 100, 100], alpha=0.25)
    assert values == [0, 25, 43.75]


def test_disabled_tracking_uses_fixed_center_crop() -> None:
    keyframes, width, height = center_crop(1920, 1080)

    assert (width, height) == (606, 1080)
    assert len(keyframes) == 1
    assert keyframes[0].time == 0
    assert keyframes[0].x == 656
    assert keyframes[0].y == 0
