FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1         PYTHONUNBUFFERED=1         PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update         && apt-get install -y --no-install-recommends build-essential libpq-dev         && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . /app/

ENV SECRET_KEY=docker-build-placeholder         DEBUG=False         ALLOWED_HOSTS=*
RUN python manage.py collectstatic --noinput

RUN chmod +x /app/start.sh
CMD ["/app/start.sh"]
