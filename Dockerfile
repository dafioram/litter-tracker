FROM python:3.12-slim

# Prevents Python from buffering stdout/stderr
ENV PYTHONUNBUFFERED=1

# This creates the 'app' folder inside the container to keep things tidy
WORKDIR /app

# Install dependencies (all have prebuilt wheels, so no compiler is needed)
COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=100 -r requirements.txt

# Copies 'app.py', 'templates/', etc. into the container's '/app/' folder
# (.dockerignore keeps your database and .env out of the image)
COPY . .

# EXPLICITLY create the mount point for data (good practice)
# This ensures /app/data exists inside the container
RUN mkdir -p /app/data/backups

EXPOSE 5000

# Run with 4 worker processes to handle multiple clicks/uploads at once.
# --preload imports the app once, so startup work (DB setup, secret key) runs once.
CMD ["gunicorn", "--preload", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]
