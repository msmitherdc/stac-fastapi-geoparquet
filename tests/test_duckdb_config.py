"""Tests for the DuckDB endpoint / TLS configuration.

These cover the settings that have to be applied to the DuckDB connection
itself, because none of them are picked up from the environment - notably the
CA bundle, which libcurl does not read from ``SSL_CERT_FILE``.
"""

import logging
from pathlib import Path

import pytest
from rustac import DuckdbClient  # type: ignore[attr-defined]

from stac_fastapi.geoparquet.api import configure_duckdb_client, s3_endpoint


def _setting(client: DuckdbClient, name: str) -> str | None:
    """A DuckDB setting's value, or None when it isn't registered.

    The httpfs settings only appear once that extension loads, so an
    unconfigured connection reports them as absent rather than empty.
    """
    table = client.query_to_table(
        f"SELECT value FROM duckdb_settings() WHERE name = '{name}'"
    )
    values: list[str | None] = table.column("value").to_pylist()
    return values[0] if values else None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # A scheme must be stripped: DuckDB prepends its own, so passing a URL
        # builds `https://bucket.https://host/key`.
        ("https://s3.us-iso-east-1.c2s.ic.gov", ("s3.us-iso-east-1.c2s.ic.gov", True)),
        ("http://localhost:9000", ("localhost:9000", False)),
        ("s3.us-iso-east-1.c2s.ic.gov", ("s3.us-iso-east-1.c2s.ic.gov", True)),
        ("localhost:9000", ("localhost:9000", True)),
        ("https://s3.example.gov/", ("s3.example.gov", True)),
        ("  https://s3.example.gov  ", ("s3.example.gov", True)),
        ("", (None, True)),
    ],
)
def test_s3_endpoint_parsing(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: tuple[str | None, bool]
) -> None:
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("AWS_S3_ENDPOINT", raw)
    assert s3_endpoint() == expected


def test_s3_endpoint_falls_back_to_aws_endpoint_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AWS_S3_ENDPOINT is a DuckDB/obstore convention; AWS_ENDPOINT_URL is the
    # one the AWS SDKs read. A deployment setting only the latter still works.
    monkeypatch.delenv("AWS_S3_ENDPOINT", raising=False)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://s3.us-iso-east-1.c2s.ic.gov")
    assert s3_endpoint() == ("s3.us-iso-east-1.c2s.ic.gov", True)


def test_ssl_cert_file_reaches_duckdb(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The whole point: DuckDB's httpfs uses libcurl, which ignores
    # SSL_CERT_FILE, so it has to be forwarded onto the connection.
    bundle = tmp_path / "bundle.pem"
    bundle.write_text("")
    monkeypatch.setenv("STAC_FASTAPI_SKIP_S3_SECRET", "true")
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    client = DuckdbClient()
    assert not _setting(client, "ca_cert_file")

    configure_duckdb_client(client)
    # DuckDB reports the path resolved, so compare it that way.
    value = _setting(client, "ca_cert_file")
    assert value is not None
    assert Path(value).resolve() == bundle.resolve()


def test_url_style_reaches_duckdb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STAC_FASTAPI_SKIP_S3_SECRET", "true")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("AWS_S3_URL_STYLE", "path")
    client = DuckdbClient()
    configure_duckdb_client(client)
    assert _setting(client, "s3_url_style") == "path"


def test_no_cert_or_style_configured_leaves_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STAC_FASTAPI_SKIP_S3_SECRET", "true")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("AWS_S3_URL_STYLE", raising=False)
    client = DuckdbClient()
    configure_duckdb_client(client)
    # Left untouched: absent, or empty/default if something else loaded httpfs.
    assert _setting(client, "ca_cert_file") in (None, "")
    assert _setting(client, "s3_url_style") in (None, "vhost")
    # Applied regardless, since it's what makes repeat reads cheap.
    assert _setting(client, "parquet_metadata_cache") == "true"


def test_endpoint_with_scheme_builds_a_usable_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheme in the endpoint must not end up inside the request URL.

    Regression test for the deployed failure mode: a HEAD against
    `https://bucket.https://s3.../key`, or against the bare bucket name when
    the endpoint never reached DuckDB at all.
    """
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("AWS_S3_URL_STYLE", raising=False)
    monkeypatch.delenv("STAC_FASTAPI_SKIP_S3_SECRET", raising=False)
    monkeypatch.setenv("AWS_S3_ENDPOINT", "https://s3.example.gov")

    client = DuckdbClient()
    # A credential-chain secret needs no credentials until it's used, but the
    # endpoint is baked in at creation, which is what we're checking.
    client.execute(
        "CREATE OR REPLACE SECRET (TYPE S3, KEY_ID 'x', SECRET 'y', "
        f"ENDPOINT '{s3_endpoint()[0]}');"
    )
    with pytest.raises(Exception) as excinfo:
        client.search("s3://bucket/path.parquet")

    message = str(excinfo.value)
    assert "https://bucket.s3.example.gov/path.parquet" in message
    assert "https://bucket.https://" not in message


def test_missing_cert_bundle_is_ignored_with_a_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A bad path would otherwise leave DuckDB unable to verify anything, with
    # only an opaque certificate error to go on.
    monkeypatch.setenv("STAC_FASTAPI_SKIP_S3_SECRET", "true")
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "nope.pem"))
    client = DuckdbClient()
    with caplog.at_level(logging.WARNING):
        configure_duckdb_client(client)

    assert not _setting(client, "ca_cert_file")
    assert "not a file" in caplog.text
