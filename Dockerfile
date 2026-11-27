# Small image: install dependencies first (cached layer), then copy the code.
FROM python:3.12-slim

WORKDIR /app

# Do not write .pyc files; print output straight away (useful in CI logs).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt pyproject.toml README.md ./
RUN pip install --no-cache-dir pyyaml langgraph

COPY pipelinedoctor ./pipelinedoctor
RUN pip install --no-cache-dir --no-deps .

# Run as a non-root user.
RUN useradd --create-home doctor
USER doctor

ENTRYPOINT ["pipelinedoctor"]
CMD ["--help"]
