# JNPA UC-III dashboard (Sub-Criterion 4). Multi-stage: build the Vite SPA, then
# serve the static bundle + reverse-proxy /api with nginx on :3000.
# ---------------------------------------------------------------------------
FROM node:20-alpine AS build
WORKDIR /app

# Install deps first (cached layer).
COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund

# Build the SPA. VITE_MAPBOX_TOKEN is a build-time arg (free tier OK); when
# unset the app falls back to the Bhuvan WMS basemap at runtime.
COPY . .
ARG VITE_MAPBOX_TOKEN=""
ARG VITE_BHUVAN_WMS="https://bhuvan-vec1.nrsc.gov.in/bhuvan/wms"
ENV VITE_MAPBOX_TOKEN=${VITE_MAPBOX_TOKEN} \
    VITE_BHUVAN_WMS=${VITE_BHUVAN_WMS}
RUN npm run build

# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine AS serve
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD wget -qO- http://localhost:3000/ >/dev/null 2>&1 || exit 1
CMD ["nginx", "-g", "daemon off;"]
