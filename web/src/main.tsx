import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { SocketProvider } from "./hooks/SocketContext";
import { ScenarioProvider } from "./hooks/ScenarioContext";

// --- Design-system + map foundation (load BEFORE app styles) ---
// Calcite light design system. defineCustomElements() registers the Calcite web
// components (lazily, on first use) — the React wrappers only bind to the tag
// names, so without this the elements never upgrade and the shell stays blank.
import { setAssetPath as setCalciteAssetPath } from "@esri/calcite-components";
import { defineCustomElements as defineCalcite } from "@esri/calcite-components/loader";
import "@esri/calcite-components/dist/calcite/calcite.css";
// ArcGIS Maps SDK config + light theme chrome (attribution / zoom widgets).
import esriConfig from "@arcgis/core/config";
import "@arcgis/core/assets/esri/themes/light/main.css";
// i18next (en / hi / mr — Corrigendum 3, Appendix A6).
import "./i18n";

import "maplibre-gl/dist/maplibre-gl.css";
import "./index.css";

// Pin Calcite + ArcGIS asset paths to the matching CDN versions so icons,
// translations and the WebGL workers resolve without bundling the asset tree.
setCalciteAssetPath("https://js.arcgis.com/calcite-components/3.3.3/assets");
esriConfig.assetsPath = "https://js.arcgis.com/4.34/@arcgis/core/assets";

// Register the Calcite custom elements (lazy). Must run before React renders
// the Calcite-based shell.
defineCalcite();

// Calcite reads colour mode from a class on an ancestor; mark the document so
// the whole tree (including portalled Calcite overlays) renders light.
document.documentElement.classList.add("calcite-mode-light");

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Live control room: keep data fresh but don't hammer the gateway. Most
      // screens also receive push updates over the WebSocket.
      refetchInterval: 5_000,
      refetchOnWindowFocus: false,
      staleTime: 2_000,
      retry: 1,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <ScenarioProvider>
          <SocketProvider>
            <App />
          </SocketProvider>
        </ScenarioProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
);
