# Playwright ka official image use kar rahe hain jisme browsers pehle se hain
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

# Poppler-utils (PDF processing ke liye) install kar rahe hain
RUN apt-get update && apt-get install -y poppler-utils && rm -rf /var/lib/apt/lists/*

# Work directory set karein
WORKDIR /app

# Files copy karein
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Baaki sara code copy karein
COPY . .

# Bot start karne ki command
CMD ["python", "main.py"]
