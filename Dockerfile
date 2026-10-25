FROM python:3.11-slim
RUN pip install --no-cache-dir pytest==8.3.5
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
ENTRYPOINT ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
