import { useState, useCallback, useRef, useEffect } from "react";
import type { RunStep } from "@/lib/types";
import { apiFetch } from "@/lib/api";

interface StartRunResponse {
  run_id?: string;
  booking_id?: string;
  sandbox_id?: string;
  novnc_url?: string;
  status: string;
}

export function useRun() {
  const [runId, setRunId] = useState<string | null>(null);
  const [status, setStatus] = useState<string>("idle");
  const [steps, setSteps] = useState<RunStep[]>([]);
  const [currentStep, setCurrentStep] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [novncUrl, setNovncUrl] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const cleanup = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
  }, []);

  useEffect(() => cleanup, [cleanup]);

  const connectWs = useCallback(
    (id: string) => {
      cleanup();
      const protocol = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(
        `${protocol}//${location.host}/ws/runs/${id}`,
      );
      wsRef.current = ws;

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.status === "completed") {
            setStatus("completed");
            setSteps((prev) => [
              ...prev,
              {
                status: "completed",
                message: data.message || "Done",
                timestamp: data.timestamp || new Date().toISOString(),
              },
            ]);
            ws.close();
          } else if (data.status === "failed") {
            setStatus("failed");
            setError(data.message || "Run failed");
            setSteps((prev) => [
              ...prev,
              {
                status: "failed",
                message: data.message || "Failed",
                timestamp: data.timestamp || new Date().toISOString(),
              },
            ]);
            ws.close();
          } else {
            setStatus(data.status || "running");
            setCurrentStep(data.message || data.status);
            setSteps((prev) => [
              ...prev,
              {
                status: data.status,
                message: data.message || data.status,
                timestamp: data.timestamp || new Date().toISOString(),
              },
            ]);
          }
        } catch {
          // ignore malformed messages
        }
      };

      ws.onerror = () => {
        setError("WebSocket connection error");
        setStatus("failed");
      };
    },
    [cleanup],
  );

  // Also try the booking WebSocket endpoint for backwards compat
  const connectBookingWs = useCallback(
    (id: string) => {
      cleanup();
      const protocol = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(
        `${protocol}//${location.host}/ws/booking/${id}`,
      );
      wsRef.current = ws;

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.status === "completed") {
            setStatus("completed");
            setSteps((prev) => [
              ...prev,
              {
                status: "completed",
                message: data.message || "Done",
                timestamp: data.timestamp || new Date().toISOString(),
              },
            ]);
            ws.close();
          } else if (data.status === "failed") {
            setStatus("failed");
            setError(data.message || "Run failed");
            setSteps((prev) => [
              ...prev,
              {
                status: "failed",
                message: data.message || "Failed",
                timestamp: data.timestamp || new Date().toISOString(),
              },
            ]);
            ws.close();
          } else {
            setStatus(data.status || "running");
            setCurrentStep(data.message || data.status);
            setSteps((prev) => [
              ...prev,
              {
                status: data.status,
                message: data.message || data.status,
                timestamp: data.timestamp || new Date().toISOString(),
              },
            ]);
          }
        } catch {
          // ignore
        }
      };

      ws.onerror = () => {
        setError("WebSocket connection error");
        setStatus("failed");
      };
    },
    [cleanup],
  );

  const startRun = useCallback(
    async (flowId: string, inputs: Record<string, string>) => {
      setSteps([]);
      setError(null);
      setCurrentStep(null);
      setStatus("starting");

      try {
        // Try the generic /api/runs endpoint first
        const data = await apiFetch<StartRunResponse>("/api/runs", {
          method: "POST",
          body: JSON.stringify({ flow_id: flowId, inputs }),
        });
        const id = data.run_id || data.booking_id || "";
        setRunId(id);
        setNovncUrl(data.novnc_url || null);
        connectWs(id);
      } catch {
        // Fall back to booking endpoint for uber-specific flows
        try {
          const data = await apiFetch<StartRunResponse>(
            "/api/booking/start",
            {
              method: "POST",
              body: JSON.stringify(inputs),
            },
          );
          const id = data.booking_id || "";
          setRunId(id);
          setNovncUrl(data.novnc_url || null);
          connectBookingWs(id);
        } catch (err) {
          setError(err instanceof Error ? err.message : "Failed to start run");
          setStatus("failed");
        }
      }
    },
    [connectWs, connectBookingWs],
  );

  const reset = useCallback(() => {
    cleanup();
    setRunId(null);
    setStatus("idle");
    setSteps([]);
    setCurrentStep(null);
    setError(null);
    setNovncUrl(null);
  }, [cleanup]);

  return {
    runId,
    status,
    steps,
    currentStep,
    error,
    novncUrl,
    startRun,
    reset,
  };
}
