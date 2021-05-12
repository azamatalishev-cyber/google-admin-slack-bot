FROM python:3

COPY ./app /app

COPY ./config /app/config

WORKDIR /app
RUN pip install --no-cache-dir -r requirements.txt

RUN chown -R 1000:1000 /app
RUN chmod 755 /app
USER 1000
CMD ["gunicorn", "-c", "config/gunicorn.conf.py", "wsgi:app"]
