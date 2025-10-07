FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Ensure data dir exists
RUN mkdir -p /app/data
CMD ["python","job_scraper.py"]
