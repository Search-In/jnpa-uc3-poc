import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { registerServiceWorker } from "./lib/pwa";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);

// Register the service worker (precache + WebPush handlers). Best-effort; the
// app runs fine without it (push/offline degrade to live feed + network).
registerServiceWorker();
