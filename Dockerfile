FROM python:3.11-slim

# Install system dependencies (including FFmpeg, git, and Node.js)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY . .

# Port exposed for Render health checks
EXPOSE 8080

# Command to run the bot
CMD ["python", "bot.py"]
