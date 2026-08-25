FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY nebula ./nebula

EXPOSE 8000

CMD ["uvicorn", "nebula.api:app", "--host", "0.0.0.0", "--port", "8000"]
