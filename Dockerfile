FROM python:3.12-slim

WORKDIR /app

# Dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# The poller runs in burst mode and exits when the burst ends;
# compose restarts it (restart: unless-stopped), so it runs forever.
CMD ["python", "poller.py", "--burst", "--minutes", "720"]
