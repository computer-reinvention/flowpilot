import { useState, useEffect, useCallback } from "react";
import type { Flow } from "@/lib/types";
import { apiFetch } from "@/lib/api";

export function useFlows(enabled = true) {
  const [flows, setFlows] = useState<Flow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchFlows = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch<Flow[]>("/api/flows");
      setFlows(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch flows");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (enabled) fetchFlows();
    else setLoading(false);
  }, [enabled, fetchFlows]);

  const createFlow = useCallback(
    async (data: {
      name: string;
      domain: string;
      task: string;
      entry_url?: string;
      icon?: string;
    }) => {
      const created = await apiFetch<Flow>("/api/flows", {
        method: "POST",
        body: JSON.stringify(data),
      });
      setFlows((prev) => [...prev, created]);
      return created;
    },
    [],
  );

  const updateFlow = useCallback(
    async (flowId: string, updates: Record<string, unknown>) => {
      const updated = await apiFetch<Flow>(`/api/flows/${flowId}`, {
        method: "PUT",
        body: JSON.stringify(updates),
      });
      setFlows((prev) => prev.map((f) => (f.id === flowId ? updated : f)));
      return updated;
    },
    [],
  );

  const deleteFlow = useCallback(
    async (flowId: string) => {
      await apiFetch(`/api/flows/${flowId}`, { method: "DELETE" });
      setFlows((prev) => prev.filter((f) => f.id !== flowId));
    },
    [],
  );

  return { flows, loading, error, refetch: fetchFlows, createFlow, updateFlow, deleteFlow };
}
