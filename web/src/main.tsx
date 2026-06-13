import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { SocketProvider } from "./hooks/SocketContext";
import { ScenarioProvider } from "./hooks/ScenarioContext";
import "maplibre-gl/dist/maplibre-gl.css";
import "./index.css";

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
