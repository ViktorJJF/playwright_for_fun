# Use a Python base image
FROM python:3.12-slim

# Install system dependencies required by Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg libnss3 libatk-bridge2.0-0 libxcomposite1 libxrandr2 libgbm1 libgtk-3-0 libxdamage1 libasound2 libxtst6 libx11-xcb1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python and Playwright
RUN pip install --no-cache-dir playwright

# Install Playwright browsers
RUN playwright install

# Set the working directory
WORKDIR /app

# Copy the application code
COPY . /app

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port 5000
EXPOSE 5000

# Run the application with uvicorn on port 5000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5000"]
