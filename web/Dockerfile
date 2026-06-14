# JNPA UC-III dashboard (Sub-Criterion 4) + trucking-app PWA (Prompt 11).
# Build context is the REPO ROOT (see docker-compose web.build.context: .) so
# this image can build BOTH the dashboard (web/) and the driver PWA (mobile-pwa/)
# and serve them from one nginx: the dashboard at / and the PWA at /pwa.
# ---------------------------------------------------------------------------
FROM node:20-alpine AS build-web
WORKDIR /app
COPY web/package.json web/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY web/ .
ARG VITE_MAPBOX_TOKEN=""
ARG VITE_BHUVAN_WMS="https://bhuvan-vec1.nrsc.gov.in/bhuvan/wms"
ENV VITE_MAPBOX_TOKEN=${VITE_MAPBOX_TOKEN} \
    VITE_BHUVAN_WMS=${VITE_BHUVAN_WMS}
RUN npm run build

# ---------------------------------------------------------------------------
FROM node:20-alpine AS build-pwa
WORKDIR /app
COPY mobile-pwa/package.json mobile-pwa/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY mobile-pwa/ .
# Built under /pwa so the service-worker scope + asset URLs match where nginx
# mounts it. The PWA always calls the relative /api path (nginx proxies it).
ENV PWA_BASE="/pwa/"
RUN npm run build

# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine AS serve
COPY web/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build-web /app/dist /usr/share/nginx/html
COPY --from=build-pwa /app/dist /usr/share/nginx/html/pwa
EXPOSE 3000
# nginx serves on :3000 (HTTP 200) but this image's BusyBox wget exits non-zero
# on `-qO-`, so the old check flagged a healthy container unhealthy. Use curl,
# which is present in this image and honours --fail for a true status check.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD curl -fsS http://localhost:3000/ >/dev/null 2>&1 || exit 1
CMD ["nginx", "-g", "daemon off;"]
