"""Sprint 16: live matching prefers age fit over geography when scores conflict."""

from types import SimpleNamespace

import pytest

from apps.events.geography import geographic_score
from apps.events.live_matching import (
    _AGE_PREF_SCALE,
    _GEOGRAPHY_SCALE,
    _age_preference_score,
)


def _participant(*, age, pref_min=None, pref_max=None, country="NZ", city="Auckland"):
    return SimpleNamespace(
        age=age,
        age_pref_min=pref_min,
        age_pref_max=pref_max,
        country=country,
        city=city,
    )


def test_age_overlap_beats_geography_when_prefs_conflict():
    """Prefer mutual age fit over the same geography when ages fall outside prefs."""
    partner = _participant(
        age=32, pref_min=28, pref_max=38, country="NZ", city="Auckland"
    )
    age_fit = _participant(
        age=30, pref_min=28, pref_max=35, country="NZ", city="Auckland"
    )
    age_misfit = _participant(
        age=30, pref_min=18, pref_max=25, country="NZ", city="Auckland"
    )
    geo = geographic_score(age_fit, partner)
    assert geo > 0

    weight_with_age = _age_preference_score(age_fit, partner) * _AGE_PREF_SCALE + geo * _GEOGRAPHY_SCALE
    weight_without_age = _age_preference_score(age_misfit, partner) * _AGE_PREF_SCALE + geo * _GEOGRAPHY_SCALE
    assert weight_with_age > weight_without_age