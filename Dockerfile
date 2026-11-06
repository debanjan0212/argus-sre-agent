FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl git ca-certificates \
 && curl -fsSL -o /usr/local/bin/kubectl \
    "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/$(dpkg --print-architecture)/kubectl" \
 && chmod +x /usr/local/bin/kubectl && apt-get purge -y curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY argus ./argus
RUN pip install --no-cache-dir .
USER 10001
ENV PYTHONUNBUFFERED=1 HOME=/tmp
EXPOSE 8080
CMD ["argus", "serve"]
