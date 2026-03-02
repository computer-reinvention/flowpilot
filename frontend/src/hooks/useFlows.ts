import { useState, useEffect, useCallback } from "react";
import type { Flow } from "@/lib/types";
import { apiFetch } from "@/lib/api";

export function useFlows() {
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
    fetchFlows();
  }, [fetchFlows]);

  return { flows, loading, error, refetch: fetchFlows };
}
