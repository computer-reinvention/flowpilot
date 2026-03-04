import { useState, useEffect, useCallback } from "react";
import type { TriggerHook } from "@/lib/types";
import { apiFetch } from "@/lib/api";

export function useTriggerHooks(flowId: string) {
  const [hooks, setHooks] = useState<TriggerHook[]>([]);
  const [loading, setLoading] = useState(true);

  const fetchHooks = useCallback(async () => {
    setLoading(true);
    try {
      const data = await apiFetch<TriggerHook[]>(`/api/v1/flows/${flowId}/hooks`);
      setHooks(data);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, [flowId]);

  useEffect(() => {
    fetchHooks();
  }, [fetchHooks]);

  const createHook = useCallback(
    async (name: string): Promise<TriggerHook> => {
      const hook = await apiFetch<TriggerHook>(`/api/v1/flows/${flowId}/hooks`, {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      setHooks((prev) => [hook, ...prev]);
      return hook;
    },
    [flowId],
  );

  const deleteHook = useCallback(
    async (hookId: string) => {
      await apiFetch(`/api/v1/flows/${flowId}/hooks/${hookId}`, { method: "DELETE" });
      setHooks((prev) => prev.filter((h) => h.id !== hookId));
    },
    [flowId],
  );

  return { hooks, loading, createHook, deleteHook, refetch: fetchHooks };
}
