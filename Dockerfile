# Includes Python + Chromium/Firefox/WebKit + all OS deps preinstalled
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

# Security: avoid running as root at runtime
# (base image provides "pwuser")
WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app
COPY . /app

# Env defaults (override in Render dashboard)
ENV RUN_FOREVER=1 \
    RUN_EVERY_SECONDS=300

# Start your watcher
CMD ["python","seekube_telegram_watcher.py"]
