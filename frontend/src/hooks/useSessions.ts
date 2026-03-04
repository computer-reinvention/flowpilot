import { useState, useEffect, useCallback } from "react";
import type { Session } from "@/lib/types";
import { apiFetch, ApiError } from "@/lib/api";

export function useSessions(enabled = true) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchSessions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch<Session[]>("/api/sessions");
      setSessions(data);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setSessions([]);
      } else {
        setError(err instanceof Error ? err.message : "Failed to fetch sessions");
        setSessions([]);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (enabled) fetchSessions();
    else setLoading(false);
  }, [enabled, fetchSessions]);

  const deleteSession = useCallback(async (id: string) => {
    await apiFetch(`/api/sessions/${id}`, { method: "DELETE" });
    setSessions((prev) => prev.filter((s) => s.id !== id));
  }, []);

  return { sessions, loading, error, refetch: fetchSessions, deleteSession };
}
