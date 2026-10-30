# ui/Dockerfile
# Builds the Streamlit frontend image for Incident Copilot.
# Uses Python 3.11-slim to keep the image lightweight.
# Runs as a non-root user (appuser) for security best practice.
# NOTE: python:3.11-slim does NOT include curl; healthchecks use Python urllib.

FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy and install Python dependencies first (layer-cache friendly)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source code
COPY app/ ./app/

# Create a non-root user and switch to it before running the app
RUN adduser --disabled-password --gecos "" appuser
USER appuser

# Streamlit listens on 8501 by default
EXPOSE 8501

# Disable Streamlit's browser-open behaviour (not useful inside Docker)
# and set the server address so it's reachable from outside the container
CMD ["streamlit", "run", "app/main.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]

