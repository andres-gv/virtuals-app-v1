# 1. Use an official Python runtime as a parent image
FROM python:3.11-slim

# 2. Set the working directory in the container
WORKDIR /app

# 3. Install system dependencies required by Playwright's browser (Chromium)
# This is crucial for the headless browser to function in a minimal environment.
RUN apt-get update && apt-get install -y \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# 4. Copy the requirements file into the container
COPY requirements.txt .

# 5. Install the Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# 6. Install the Playwright browser itself inside the container
RUN playwright install chromium --with-deps

# 7. Copy your application code into the container
COPY app.py .
COPY styles.css .

# 8. Expose the port the app runs on
EXPOSE 8000

# 9. Define the command to run your app when the container starts
# Use --host 0.0.0.0 to make it accessible from outside the container
CMD ["shiny", "run", "--host", "0.0.0.0", "--port", "8000", "app.py"]