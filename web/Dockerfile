# JNPA UC-III dashboard + trucking-app PWA (Production Image).
# Build context is the REPO ROOT. Copy pre-built dist folders directly
# to avoid compilation and OOM issues on the host.
# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine

COPY web/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY web/dist /usr/share/nginx/html
COPY mobile-pwa/dist /usr/share/nginx/html/pwa

EXPOSE 80 443

# nginx serves on :80 and :443. Use curl to verify health status via HTTPS (ignoring cert checks).
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD curl -kfsS https://localhost/ >/dev/null 2>&1 || exit 1

CMD ["nginx", "-g", "daemon off;"]
