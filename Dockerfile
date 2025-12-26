FROM python:3.9-slim

# Prevents Python from buffering stdout/stderr
ENV PYTHONUNBUFFERED=1

# This creates the 'app' folder inside the container to keep things tidy
WORKDIR /app

# Install dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=100 -r requirements.txt

# Copies 'app.py', 'data/', etc. into the container's '/app/' folder
COPY . .

# EXPLICITLY create the mount point for data (good practice)
# This ensures /app/data exists inside the container
RUN mkdir -p /app/data/backups

EXPOSE 5000

# Run with 4 worker processes to handle multiple clicks/uploads at once
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]