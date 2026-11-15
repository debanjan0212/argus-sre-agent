FROM python:3.12-slim
# kubectl is checked against the checksum Kubernetes publishes next to it, so a tampered or
# truncated download fails the build instead of ending up in the image.
RUN apt-get update && apt-get install -y --no-install-recommends curl git ca-certificates \
 && base="https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/$(dpkg --print-architecture)" \
 && curl -fsSL -o /usr/local/bin/kubectl "$base/kubectl" \
 && echo "$(curl -fsSL "$base/kubectl.sha256")  /usr/local/bin/kubectl" | sha256sum -c - \
 && chmod +x /usr/local/bin/kubectl && apt-get purge -y curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY argus ./argus
RUN pip install --no-cache-dir .
USER 10001
ENV PYTHONUNBUFFERED=1 HOME=/tmp
EXPOSE 8080
CMD ["argus", "serve"]
