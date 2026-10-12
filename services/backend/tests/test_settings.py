import pytest

from listen import appsettings


def test_reading_speed_defaults_to_300_words_per_minute():
    assert appsettings.DEFAULTS["reading_wpm"] == 300


@pytest.mark.parametrize("value", [150, 300, 800, 299.0])
def test_reading_speed_accepts_whole_numbers_in_range(value):
    clean = appsettings.validate({"reading_wpm": value})
    assert clean == {"reading_wpm": int(value)}
    assert isinstance(clean["reading_wpm"], int)


@pytest.mark.parametrize("value", [149, 801, 0, -5, "300", None, True, [300]])
def test_reading_speed_rejects_other_values(value):
    with pytest.raises(ValueError, match="reading_wpm"):
        appsettings.validate({"reading_wpm": value})
