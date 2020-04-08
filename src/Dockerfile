FROM amsterdam/python:3.8-buster AS builder
MAINTAINER datapunt@amsterdam.nl

RUN pip install --no-cache-dir amsterdam-dso-api

# Start runtime image,
FROM amsterdam/python:3.8-slim-buster

# Copy python build artifacts from builder image
COPY --from=builder /usr/local/bin/ /usr/local/bin/
COPY --from=builder /usr/local/lib/python3.8/site-packages/ /usr/local/lib/python3.8/site-packages/


WORKDIR /app
COPY . ./

ENV DJANGO_SETTINGS_MODULE=dso_api.settings DJANGO_DEBUG=false

USER datapunt
CMD ["python manage run_import bagh"]
