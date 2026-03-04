import { useState, useEffect, useCallback, useMemo } from "react";
import type { Run, RunStep } from "@/lib/types";
import { ACTIVE_RUN_STATUSES } from "@/lib/types";
import { apiFetch } from "@/lib/api";

interface RawRun {
  id: string;
  flow_id: string;
  user_id: string;
  status: string;
  inputs: string;
  sandbox_id?: string;
  steps: string;
  current_step?: string;
  result?: string;
  error?: string;
  created_at: string;
  completed_at?: string;
}

function parseRun(raw: RawRun): Run {
  let inputs: Record<string, string> = {};
  let steps: RunStep[] = [];
  let result: Record<string, unknown> | undefined;
  try {
    inputs = JSON.parse(raw.inputs || "{}");
  } catch { /* empty */ }
  try {
    steps = JSON.parse(raw.steps || "[]");
  } catch { /* empty */ }
  try {
    result = raw.result ? JSON.parse(raw.result) : undefined;
  } catch { /* empty */ }
  return {
    ...raw,
    status: raw.status as Run["status"],
    inputs,
    steps,
    result,
  };
}

export function useRuns(enabled = true) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchRuns = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch<RawRun[]>("/api/runs");
      setRuns(data.map(parseRun));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch runs");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (enabled) fetchRuns();
    else setLoading(false);
  }, [enabled, fetchRuns]);

  const activeRuns = useMemo(() => {
    const activeStatuses = new Set<string>(ACTIVE_RUN_STATUSES);
    return runs.filter((r) => activeStatuses.has(r.status));
  }, [runs]);

  return { runs, activeRuns, loading, error, refetch: fetchRuns };
}
