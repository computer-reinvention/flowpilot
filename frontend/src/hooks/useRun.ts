import { useState, useCallback, useRef, useEffect } from "react";
import type { RunStep } from "@/lib/types";
import { ACTIVE_RUN_STATUSES } from "@/lib/types";
import { apiFetch } from "@/lib/api";
import {
  requestNotificationPermission,
  sendHITLNotification,
  stopTitleFlash,
} from "@/lib/notifications";

interface StartRunResponse {
  run_id: string;
  sandbox_id?: string;
  status: string;
}

interface RunDetailResponse {
  id: string;
  flow_id: string;
  user_id: string;
  status: string;
  inputs: Record<string, string>;
  sandbox_id?: string;
  steps: RunStep[];
  current_step?: string;
  result?: Record<string, unknown>;
  error?: string;
  created_at: string;
  completed_at?: string;
}

interface WsMessage {
  status?: string;
  message?: string;
  timestamp?: string;
  steps?: RunStep[];
  current_step?: string;
  error?: string;
}

export function useRun() {
  const [runId, setRunId] = useState<string | null>(null);
  const [sandboxId, setSandboxId] = useState<string | null>(null);
  const [status, setStatus] = useState<string>("idle");
  const [steps, setSteps] = useState<RunStep[]>([]);
  const [currentStep, setCurrentStep] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
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
          const data = JSON.parse(event.data) as WsMessage;

          // Handle initial state message from reconnection (has steps array)
          if (data.steps && Array.isArray(data.steps)) {
            setSteps(data.steps);
            setStatus(data.status || "running");
            setCurrentStep(data.current_step || null);
            if (data.error) setError(data.error);
            return;
          }

          if (data.status === "completed") {
            setStatus("completed");
            stopTitleFlash();
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
            stopTitleFlash();
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
            sendHITLNotification(data.message || "Login required");
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
          } else if (data.status === "cancelled") {
            setStatus("cancelled");
            stopTitleFlash();
            ws.close();
          } else {
            const resolvedStatus = data.status || "running";
            setStatus(resolvedStatus);
            stopTitleFlash();
            setCurrentStep(data.message || resolvedStatus);
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
                  status: resolvedStatus,
                  message: data.message || resolvedStatus,
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
    async (flowId: string, inputs: Record<string, string>): Promise<string | null> => {
      setSteps([]);
      setError(null);
      setCurrentStep(null);
      setStatus("starting");

      // Request notification permission on first run
      requestNotificationPermission();

      try {
        const data = await apiFetch<StartRunResponse>("/api/runs", {
          method: "POST",
          body: JSON.stringify({ flow_id: flowId, inputs }),
        });
        const id = data.run_id;
        if (!id) throw new Error("Server did not return a run_id");
        setRunId(id);
        setSandboxId(data.sandbox_id || null);
        connectWs(id);
        return id;
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to start run");
        setStatus("failed");
        return null;
      }
    },
    [connectWs],
  );

  const reconnectToRun = useCallback(
    async (id: string) => {
      setError(null);
      setStatus("connecting");

      try {
        const data = await apiFetch<RunDetailResponse>(`/api/runs/${id}`);
        setRunId(data.id);
        setSteps(data.steps || []);
        setStatus(data.status);
        setCurrentStep(data.current_step || null);
        setSandboxId(data.sandbox_id || null);
        if (data.error) setError(data.error);

        // Connect WS if run is still active
        if ((ACTIVE_RUN_STATUSES as readonly string[]).includes(data.status)) {
          connectWs(data.id);
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to reconnect");
        setStatus("failed");
      }
    },
    [connectWs],
  );

  const resumeRun = useCallback(async () => {
    if (!runId) return;
    try {
      stopTitleFlash();
      await apiFetch(`/api/runs/${runId}/resume`, { method: "POST" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to resume run");
    }
  }, [runId]);

  const cancelRun = useCallback(async () => {
    if (!runId) return;
    try {
      stopTitleFlash();
      await apiFetch(`/api/runs/${runId}/cancel`, { method: "POST" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to cancel run");
    }
  }, [runId]);

  const reset = useCallback(() => {
    cleanup();
    stopTitleFlash();
    setRunId(null);
    setStatus("idle");
    setSteps([]);
    setCurrentStep(null);
    setSandboxId(null);
    setError(null);
  }, [cleanup]);

  return {
    runId,
    sandboxId,
    status,
    steps,
    currentStep,
    error,
    startRun,
    reconnectToRun,
    resumeRun,
    cancelRun,
    reset,
  };
}
