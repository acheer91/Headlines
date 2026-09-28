# Builds the web app, then runs the API serving it from the same origin.
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm install
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /srv
COPY api/requirements.txt api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt
COPY api/ api/
COPY config/ config/
COPY db/ db/
COPY --from=web /web/dist web/dist
ENV WEB_DIST=/srv/web/dist FAVORITES_FILE=/srv/config/favorites.json MIGRATIONS_DIR=/srv/db/migrations
WORKDIR /srv/api
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
