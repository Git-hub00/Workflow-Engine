# Web image — builds the React/Vite SPA, then serves it with nginx and
# reverse-proxies /api/ to the API container. Replaces the host nginx + host
# `npm run build` steps from the systemd deployment.
#
# Build context = repo root:  docker build -f deploy/docker/web.Dockerfile .
# The VITE_* build args bake the PUBLIC (browser-facing) URLs into the bundle.

# --- Stage 1: build the SPA ------------------------------------------------
FROM node:20-slim AS build
WORKDIR /spa

# Public URLs the browser uses. Keycloak is reached DIRECTLY by the browser, so
# this must be the public host (not the internal docker service name).
ARG VITE_API_BASE_URL=/api
ARG VITE_KEYCLOAK_URL=http://localhost:8081
ARG VITE_KEYCLOAK_REALM=workflow
ARG VITE_KEYCLOAK_CLIENT_ID=workflow-spa
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL \
    VITE_KEYCLOAK_URL=$VITE_KEYCLOAK_URL \
    VITE_KEYCLOAK_REALM=$VITE_KEYCLOAK_REALM \
    VITE_KEYCLOAK_CLIENT_ID=$VITE_KEYCLOAK_CLIENT_ID

# Install deps against the lockfile (fresh, Linux-native — avoids any host
# node_modules that were built for a different OS).
COPY services/spa/package.json services/spa/package-lock.json ./
RUN npm ci

COPY services/spa/ ./
RUN npm run build

# --- Stage 2: serve with nginx ---------------------------------------------
FROM nginx:1.27-alpine
COPY deploy/docker/web-nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /spa/dist /usr/share/nginx/html
EXPOSE 80
