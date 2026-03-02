import { useState, useCallback, useRef, useEffect } from "react";
import type { RunStep } from "@/lib/types";
import { apiFetch } from "@/lib/api";

interface StartRunResponse {
  run_id: string;
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
          } else if (data.status === "paused_for_auth") {
            setStatus("paused_for_auth");
            setCurrentStep(data.message || "Login required");
            setSteps((prev) => [
              ...prev,
              {
                status: "paused_for_auth",
                message: data.message || "Login required",
                timestamp: data.timestamp || new Date().toISOString(),
              },
            ]);
            // Do NOT close WebSocket — we need it for resume updates
          } else {
            setStatus(data.status || "running");
            setCurrentStep(data.message || data.status);
            // Mark previous running/connecting steps as done, append new one
            setSteps((prev) => {
              const settled = prev.map((s) =>
                s.status === "running" || s.status === "connecting"
                  ? { ...s, status: "done" }
                  : s,
              );
              return [
                ...settled,
                {
                  status: data.status,
                  message: data.message || data.status,
                  timestamp: data.timestamp || new Date().toISOString(),
                },
              ];
            });
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

  const startRun = useCallback(
    async (flowId: string, inputs: Record<string, string>) => {
      setSteps([]);
      setError(null);
      setCurrentStep(null);
      setStatus("starting");

      try {
        const data = await apiFetch<StartRunResponse>("/api/runs", {
          method: "POST",
          body: JSON.stringify({ flow_id: flowId, inputs }),
        });
        const id = data.run_id || "";
        setRunId(id);
        setNovncUrl(data.novnc_url || null);
        connectWs(id);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to start run");
        setStatus("failed");
      }
    },
    [connectWs],
  );

  const resumeRun = useCallback(async () => {
    if (!runId) return;
    try {
      await apiFetch(`/api/runs/${runId}/resume`, { method: "POST" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to resume run");
    }
  }, [runId]);

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
    resumeRun,
    reset,
  };
}
