FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .
ENV DB_PATH=/app/data/bot.db PYTHONUNBUFFERED=1
VOLUME /app/data
CMD ["python", "bot.py"]
