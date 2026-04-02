"""Tests for URL normalization."""

from src.core.urls import normalize_url


def test_trailing_slash_stripped() -> None:
    assert normalize_url("https://demo-bank-2.vercel.app/") == normalize_url(
        "https://demo-bank-2.vercel.app"
    )


def test_case_insensitive_host() -> None:
    assert normalize_url("https://DEMO-BANK-2.Vercel.App/") == normalize_url(
        "https://demo-bank-2.vercel.app"
    )


def test_www_stripped() -> None:
    assert normalize_url("https://www.mybank.com/login") == normalize_url(
        "https://mybank.com/login"
    )


def test_default_https_port_stripped() -> None:
    assert normalize_url("https://bank.com:443/login") == normalize_url("https://bank.com/login")


def test_default_http_port_stripped() -> None:
    assert normalize_url("http://bank.com:80/login") == normalize_url("http://bank.com/login")


def test_non_default_port_preserved() -> None:
    result = normalize_url("https://bank.com:8443/login")
    assert ":8443" in result


def test_path_preserved() -> None:
    result = normalize_url("https://bank.com/online/login")
    assert "/online/login" in result


def test_query_preserved() -> None:
    result = normalize_url("https://bank.com/login?redirect=dashboard")
    assert "redirect=dashboard" in result


def test_fragment_dropped() -> None:
    result = normalize_url("https://bank.com/login#section")
    assert "#" not in result


def test_whitespace_stripped() -> None:
    assert normalize_url("  https://bank.com/login  ") == normalize_url("https://bank.com/login")


def test_real_world_trailing_slash_problem() -> None:
    """The exact case that caused duplicate connections in our DB."""
    url_with_slash = "https://demo-bank-2.vercel.app/"
    url_without_slash = "https://demo-bank-2.vercel.app"
    assert normalize_url(url_with_slash) == normalize_url(url_without_slash)
    assert normalize_url(url_with_slash) == "https://demo-bank-2.vercel.app"
