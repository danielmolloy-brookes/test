FROM python:3.11-slim

# System deps for Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libjpeg-dev \
    zlib1g-dev \
    libfreetype6-dev \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create required directories for uploaded files
# NOTE: On Railway, mount a persistent volume at /app/static to survive deploys.
#       Dashboard: Project → Service → Volumes → Add Volume → Mount Path: /app/static
RUN mkdir -p static/qr_codes static/maps static/exhibitor_maps static/brand static/qr_codes

# Expose port
EXPOSE 8000

# Run with uvicorn
CMD ["python", "-m", "app.main"]
