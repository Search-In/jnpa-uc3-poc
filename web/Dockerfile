# JNPA UC-III dashboard + trucking-app PWA (Production Image).
# Build context is the REPO ROOT. Copy pre-built dist folders directly
# to avoid compilation and OOM issues on the host. The dist MUST be built in
# live mode (`vite build` defaults to live — see web/vite.config.ts); the guard
# below fails the image build if a mock bundle is ever copied in.
# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine

# Expected data mode of the bundled dashboard. The build is REFUSED if the
# copied dist does not match (defaults to live for production images).
ARG VITE_DATA_MODE=live

COPY web/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY web/dist /usr/share/nginx/html
COPY mobile-pwa/dist /usr/share/nginx/html/pwa

# ---- Build-time guard: refuse to ship a mock dashboard --------------------
# 1) the live marker MUST be present in the bundle, and
# 2) the MockAdapter sentinel MUST be absent (proves it was tree-shaken).
RUN set -e; \
    html=/usr/share/nginx/html; \
    if [ "${VITE_DATA_MODE}" = "live" ]; then \
      if ! grep -rqs "JNPA_DATA_MODE:live" "$html/assets"; then \
        echo "BUILD REFUSED: dashboard bundle is not in LIVE mode (live marker missing)." >&2; \
        echo "  Rebuild web/dist with: (cd web && npm run build)  [vite build defaults to live]" >&2; \
        exit 1; \
      fi; \
      if grep -rqs "JNPA_MOCK_ADAPTER_PRESENT_DO_NOT_SHIP" "$html/assets"; then \
        echo "BUILD REFUSED: MockAdapter is present in a LIVE production bundle." >&2; \
        exit 1; \
      fi; \
      echo "[web] live-mode bundle verified — MockAdapter tree-shaken out."; \
    else \
      echo "[web] WARNING: building image with VITE_DATA_MODE=${VITE_DATA_MODE} (non-live)."; \
    fi

EXPOSE 80 443

# nginx serves on :80 and :443. Use curl to verify health status via HTTPS (ignoring cert checks).
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD curl -kfsS https://localhost/ >/dev/null 2>&1 || exit 1

CMD ["nginx", "-g", "daemon off;"]
