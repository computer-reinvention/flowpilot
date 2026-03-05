import { useState, useEffect, useCallback } from "react";
import { apiFetch } from "@/lib/api";

export interface Profile {
  name: string;
  exists: boolean;
  size_bytes: number | null;
  last_modified: string | null;
  source: "tar" | "extension" | null;
}

export function useProfiles(enabled = true) {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchProfiles = useCallback(async () => {
    setError(null);
    try {
      const data = await apiFetch<Profile[]>("/api/profiles");
      setProfiles(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch profiles");
      setProfiles([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (enabled) fetchProfiles();
    else setLoading(false);
  }, [enabled, fetchProfiles]);

  const deleteProfile = useCallback(async (name: string) => {
    await apiFetch(`/api/profiles/${encodeURIComponent(name)}`, { method: "DELETE" });
    setProfiles((prev) => prev.filter((p) => p.name !== name));
  }, []);

  return { profiles, loading, error, deleteProfile, refetch: fetchProfiles };
}
