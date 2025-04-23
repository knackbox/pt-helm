FROM python:3.13-slim

RUN apt-get update && \
    apt-get clean

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 5000

CMD ["gunicorn", "-w", "20", "-b", "0.0.0.0:5000", "app:app"]
