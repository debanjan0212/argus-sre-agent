FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
# RELEASE_BUG=1 builds the broken "1.4.0" image used by the bad-deploy demo
ARG RELEASE_BUG=0
ENV RELEASE_BUG=${RELEASE_BUG} PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8080
CMD ["python", "app.py"]
