import { useState, useEffect, useCallback } from "react";
import type { Session } from "@/lib/types";
import { apiFetch } from "@/lib/api";

export function useSessions() {
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
      // Sessions endpoint may not exist yet — that's fine
      if (err instanceof Error && !err.message.includes("404")) {
        setError(err.message);
      }
      setSessions([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

  const deleteSession = useCallback(async (id: string) => {
    await apiFetch(`/api/sessions/${id}`, { method: "DELETE" });
    setSessions((prev) => prev.filter((s) => s.id !== id));
  }, []);

  return { sessions, loading, error, refetch: fetchSessions, deleteSession };
}
