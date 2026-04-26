from app.vision.timer_ocr import parse_timer_text


def test_parse_injury_time_format() -> None:
    assert parse_timer_text("45+2") == (47, 0)
    assert parse_timer_text("90+4") == (94, 0)


def test_parse_mm_ss_format() -> None:
    assert parse_timer_text("54:13") == (54, 13)


def test_parse_malformed_text_returns_none() -> None:
    assert parse_timer_text("HT") == (None, None)
    assert parse_timer_text("xx:yy") == (None, None)
