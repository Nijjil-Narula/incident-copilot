# api/Dockerfile
# Builds the FastAPI service image for Incident Copilot.
# Uses Python 3.11-slim to keep the image lightweight.
# Runs as a non-root user (appuser) for security best practice.
# NOTE: python:3.11-slim does NOT include curl; healthchecks use Python urllib.

FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Install PyTorch CPU first to avoid pulling heavy CUDA dependencies
RUN pip install --no-cache-dir torch==2.4.1 --index-url https://download.pytorch.org/whl/cpu

# Copy and install Python dependencies next (layer-cache friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source code
COPY app/ ./app/

# Create a non-root user, create the FAISS store and HF cache directories, and chown both —
# all BEFORE the USER instruction so we still have root permissions to do so.
# The named volumes mounted at /app/faiss_store and /app/hf_cache will be writable by appuser.
RUN adduser --disabled-password --gecos "" appuser \
    && mkdir -p /app/faiss_store /app/hf_cache \
    && chown -R appuser:appuser /app/faiss_store /app/hf_cache

USER appuser

# Expose the port uvicorn will listen on
EXPOSE 8000

# Start the FastAPI app with a single uvicorn worker.
# CRITICAL: Keep single-worker because in-memory FAISS and thread locks live in process memory.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
