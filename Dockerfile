FROM python:3.12-slim

WORKDIR /app

# Install the package first so source edits don't bust the dependency layer
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY data ./data

ENV HAZOP_HOST=0.0.0.0 \
    HAZOP_PORT=8780 \
    HAZOP_DATA=/app/data

EXPOSE 8780

CMD ["hazop-web"]
