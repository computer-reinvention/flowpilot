import { useState, useEffect, useCallback } from "react";
import type { Flow, FlowInput } from "@/lib/types";
import { apiFetch } from "@/lib/api";

interface RawFlow {
  id: string;
  name: string;
  domain: string;
  description: string;
  inputs: string;
  entry_url?: string;
  icon?: string;
  created_by: string;
  created_at: string;
}

function parseFlow(raw: RawFlow): Flow {
  let inputs: FlowInput[] = [];
  try {
    inputs = JSON.parse(raw.inputs);
  } catch {
    // leave empty
  }
  return { ...raw, inputs };
}

export function useFlows() {
  const [flows, setFlows] = useState<Flow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchFlows = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch<RawFlow[]>("/api/flows");
      setFlows(data.map(parseFlow));
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
