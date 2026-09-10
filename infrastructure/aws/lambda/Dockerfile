ARG PYTHON_VERSION=3.12

FROM public.ecr.aws/lambda/python:${PYTHON_VERSION} AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install required utilities
RUN dnf install -y findutils binutils && \
  dnf clean all && \
  rm -rf /var/cache/dnf

WORKDIR /tmp
COPY pyproject.toml pyproject.toml
COPY README.md README.md
COPY src/stac_fastapi/ src/stac_fastapi/

RUN uv pip install --compile-bytecode .[lambda] --target /asset 

# Reduce package size and remove useless files
WORKDIR /asset
RUN find . -type f -name '*.pyc' | while read f; do n=$(echo $f | sed 's/__pycache__\///' | sed 's/.cpython-[0-9]*//'); cp $f $n; done;
RUN find . -type d -a -name '__pycache__' -print0 | xargs -0 rm -rf
RUN find . -type f -a -name '*.py' -print0 | xargs -0 rm -f
RUN find . -type d -a -name 'tests' -print0 | xargs -0 rm -rf

# Strip debug symbols from compiled C/C++ code
RUN find . -type f -name '*.so*' -exec strip --strip-unneeded {} \;

COPY infrastructure/aws/lambda/handler.py /asset/handler.py


# ---------------------------------------------------------------------------
# Runtime image, for deploying the function as a container image rather than a
# zip. The CDK stack targets the `build` stage above, so this is only built
# when asked for:
#
#     docker build -f infrastructure/aws/lambda/Dockerfile --target runtime .
# ---------------------------------------------------------------------------
FROM public.ecr.aws/lambda/python:${PYTHON_VERSION} AS runtime

# Trust anchors for private CAs. `certs/` is committed empty, so this is a
# no-op unless PEM files are placed there before building. Installing
# ca-certificates explicitly guarantees both `update-ca-trust` and the bundle
# path below exist.
COPY certs/ /etc/pki/ca-trust/source/anchors/
RUN dnf install -y ca-certificates &&   dnf clean all &&   rm -rf /var/cache/dnf &&   update-ca-trust extract

# Read by boto3 and obstore directly, and forwarded to DuckDB's `ca_cert_file`
# by `api.configure_duckdb_client` - DuckDB's HTTP layer is libcurl, which
# does not consult this variable on its own.
ENV SSL_CERT_FILE=/etc/pki/tls/certs/ca-bundle.crt

COPY --from=build /asset ${LAMBDA_TASK_ROOT}/

# The base image already provides the entrypoint (the Lambda runtime interface
# client), so naming the handler here is enough - a function deployed from this
# image needs no entrypoint or command overrides.
CMD ["handler.handler"]
