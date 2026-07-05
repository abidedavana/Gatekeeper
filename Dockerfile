# Gatekeeper container image.
# Build:  docker build -t gatekeeper .
# Usage:  docker run --rm -v "$PWD:/work" gatekeeper audit /work/requirements.txt
# Cache persists across runs if you mount it:
#         docker run --rm -v gatekeeper-cache:/home/app/.gatekeeper ... gatekeeper check requests

FROM python:3.14-slim AS build

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir build && python -m build --wheel

FROM python:3.14-slim

RUN useradd --create-home app
COPY --from=build /src/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

USER app
WORKDIR /work
ENTRYPOINT ["gatekeeper"]
CMD ["--help"]
