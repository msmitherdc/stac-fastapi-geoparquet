# Extra CA certificates

Drop PEM files here to have them trusted by the Lambda runtime image.

The `runtime` stage of `infrastructure/aws/lambda/Dockerfile` copies this
directory into the image's trust anchors and runs `update-ca-trust`, so the
resulting bundle is trusted by everything in the image — boto3, obstore, and
DuckDB alike.

DuckDB needs the extra step: its HTTP layer is libcurl, which ignores
`SSL_CERT_FILE`. `stac_fastapi.geoparquet.api.configure_duckdb_client` reads
that variable and applies it with `SET ca_cert_file`, which is what makes a
private CA work for reading geoparquet over HTTPS.

This directory is committed (near-)empty on purpose: `COPY` needs it to exist,
and an empty one is a no-op. Certificates for a specific deployment don't
belong in this repo — mount or copy them in at build time.
