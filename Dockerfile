FROM python:3.14-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY src ./src
ENTRYPOINT ["python", "-m", "src.monitor"]
CMD ["--serve"]
