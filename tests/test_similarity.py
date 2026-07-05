"""Levenshtein and substitution-pattern tests."""

from __future__ import annotations

import time

from gatekeeper.similarity import find_similar, fold_confusables, levenshtein


class TestLevenshtein:
    def test_identical(self):
        assert levenshtein("requests", "requests") == 0

    def test_empty_strings(self):
        assert levenshtein("", "") == 0
        assert levenshtein("abc", "") == 3
        assert levenshtein("", "abc") == 3

    def test_known_distances(self):
        assert levenshtein("kitten", "sitting") == 3
        assert levenshtein("flask", "flash") == 1
        assert levenshtein("requessts", "requests") == 1
        assert levenshtein("reqests", "requests") == 1

    def test_symmetric(self):
        assert levenshtein("abcdef", "azced") == levenshtein("azced", "abcdef")

    def test_max_distance_early_abandon(self):
        # true distance is 5; capped search reports max+1
        assert levenshtein("aaaaa", "bbbbb", max_distance=2) == 3
        # length difference alone exceeds the cap
        assert levenshtein("a", "abcdefgh", max_distance=2) == 3
        # within the cap, exact value is returned
        assert levenshtein("flask", "flash", max_distance=2) == 1

    def test_polynomial_not_exponential(self):
        """A naive recursive Levenshtein would take astronomically long on
        500-char inputs; the rolling-array version finishes instantly."""
        a, b = "a" * 500, "b" * 500
        start = time.perf_counter()
        assert levenshtein(a, b) == 500
        assert time.perf_counter() - start < 5.0


class TestFoldConfusables:
    def test_rn_to_m(self):
        assert fold_confusables("nurnpy") == fold_confusables("numpy")

    def test_zero_to_o(self):
        assert fold_confusables("l0dash") == fold_confusables("lodash")

    def test_one_to_l(self):
        assert fold_confusables("uti1s") == fold_confusables("utils")


class TestFindSimilar:
    def test_popular_package_is_not_its_own_typo(self):
        assert find_similar("requests", "pypi") is None
        assert find_similar("lodash", "npm") is None

    def test_pypi_canonicalization(self):
        # PEP 503: case and -/_/. are equivalent, so these ARE the popular package
        assert find_similar("Requests", "pypi") is None
        assert find_similar("python_dateutil", "pypi") is None

    def test_distance_one_typo(self):
        match = find_similar("requessts", "pypi")
        assert match is not None
        assert match.popular_name == "requests"
        assert match.distance == 1
        assert not match.substitution_pattern

    def test_distance_two_typo(self):
        match = find_similar("requesst", "pypi")
        assert match is not None
        assert match.popular_name == "requests"
        assert match.distance <= 2

    def test_substitution_pattern_detected(self):
        match = find_similar("nurnpy", "pypi")
        assert match is not None
        assert match.popular_name == "numpy"
        assert match.substitution_pattern

    def test_npm_substitution(self):
        match = find_similar("l0dash", "npm")
        assert match is not None
        assert match.popular_name == "lodash"
        assert match.substitution_pattern

    def test_unrelated_name_no_match(self):
        assert find_similar("completely-unrelated-package-xyz", "pypi") is None
