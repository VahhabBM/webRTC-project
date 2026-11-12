from types import SimpleNamespace

from apps.events.geography import (
    COUNTRY_CENTROIDS,
    MAX_SCORE,
    SAME_COUNTRY_SCORE,
    geographic_score,
)


def person(country, city=""):
    return SimpleNamespace(country=country, city=city)


def test_same_normalized_city_scores_highest_only_in_same_country():
    same_city = geographic_score(person("NZ", "  Wellington "), person("nz", "wellington"))
    different_country = geographic_score(person("NZ", "Wellington"), person("AU", "Wellington"))

    assert same_city == MAX_SCORE
    assert different_country < MAX_SCORE


def test_same_country_scores_above_foreign_countries():
    same_country = geographic_score(person("DE", "Berlin"), person("DE", "Hamburg"))
    nearby_foreign = geographic_score(person("DE"), person("FR"))

    assert same_country == SAME_COUNTRY_SCORE
    assert nearby_foreign < same_country


def test_nearby_foreign_country_scores_above_distant_country():
    nearby = geographic_score(person("DE"), person("FR"))
    distant = geographic_score(person("DE"), person("AU"))

    assert nearby > distant


def test_missing_or_unrecognized_country_is_neutral():
    assert geographic_score(person(""), person("NZ", "Wellington")) == 0
    assert geographic_score(person("ZZ"), person("NZ")) == 0
    assert geographic_score(person("AX"), person("NZ")) == 0


def test_centroid_coordinates_are_finite_and_in_range():
    assert COUNTRY_CENTROIDS
    for latitude, longitude in COUNTRY_CENTROIDS.values():
        assert -90 <= latitude <= 90
        assert -180 <= longitude <= 180
