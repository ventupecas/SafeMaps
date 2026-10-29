import pytest

from app.driving_side import DrivingSide, driving_side_for_country


@pytest.mark.parametrize(
    ("code", "side"),
    [
        ("GB", DrivingSide.LEFT),
        ("gb", DrivingSide.LEFT),
        (" JP ", DrivingSide.LEFT),
        ("AU", DrivingSide.LEFT),
        ("IN", DrivingSide.LEFT),
        ("ZA", DrivingSide.LEFT),
        ("US", DrivingSide.RIGHT),
        ("DE", DrivingSide.RIGHT),
        ("CN", DrivingSide.RIGHT),
        ("MM", DrivingSide.RIGHT),  # Myanmar switched to the right in 1970
    ],
)
def test_driving_side_by_country(code, side):
    assert driving_side_for_country(code) is side


@pytest.mark.parametrize("bad", ["", "USA", "U1", "1"])
def test_rejects_non_iso_codes(bad):
    with pytest.raises(ValueError, match="two-letter"):
        driving_side_for_country(bad)
