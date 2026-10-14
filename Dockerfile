FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
# SQLite state (topics, chat history, assignments). Mount a volume here so it survives redeploys.
VOLUME /app/data
CMD ["python", "-m", "bot"]
