# Clean-machine check: `docker build -t receipts . && docker run --rm receipts` runs the keyless demo + tests.
FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
COPY pyproject.toml requirements.lock ./
COPY receipts ./receipts
RUN pip install -r requirements.lock && pip install --no-deps -e .
COPY . .
RUN pip install --no-deps -e '.[dev]'
CMD ["sh", "-c", "make demo RECEIPTS=receipts PY=python && python -m pytest -q"]
