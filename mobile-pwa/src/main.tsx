import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { registerServiceWorker } from "./lib/pwa";
// i18next (en / hi / mr — Corrigendum 3, Appendix A6). Imported before render so
// the detected language is active on first paint.
import "./i18n";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// Register the service worker (precache + WebPush handlers). Best-effort; the
// app runs fine without it (push/offline degrade to live feed + network).
registerServiceWorker();
