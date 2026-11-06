FROM python:3.14-rc-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies required for compiling Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./

COPY . .
# Added -v for verbose output to prevent silent hanging
RUN uv pip install -v --system -e .


EXPOSE 8000

# Default FastAPI uvicorn execution
CMD ["uvicorn", "transfercore.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
