ARG THINBENCH_BASE_IMAGE=python:3.11-slim
FROM ${THINBENCH_BASE_IMAGE}
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    THINBENCH_DATA=/var/lib/thinbench \
    PORT=8000
WORKDIR /srv/thinbench
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY thinbench thinbench
COPY petro petro
COPY image_lab image_lab
COPY static static
COPY templates templates
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=4 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health').read()"]
CMD ["python", "-m", "thinbench"]
