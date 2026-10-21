import { useState, useEffect, useCallback } from "react";
import type { ApiKey } from "@/lib/types";
import { apiFetch } from "@/lib/api";

export function useApiKeys(enabled = true) {
  const [apiKeys, setApiKeys] = useState<ApiKey[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchApiKeys = useCallback(async () => {
    setError(null);
    try {
      const data = await apiFetch<ApiKey[]>("/api/v1/api-keys");
      setApiKeys(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch API keys");
      setApiKeys([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (enabled) fetchApiKeys();
    else { setApiKeys([]); setError(null); setLoading(false); }
  }, [enabled, fetchApiKeys]);

  const createApiKey = useCallback(async (name: string): Promise<ApiKey> => {
    const data = await apiFetch<ApiKey>("/api/v1/api-keys", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    const { key: _secret, ...metadata } = data;
    setApiKeys((prev) => [metadata, ...prev]);
    return data;
  }, []);

  const deleteApiKey = useCallback(async (id: string) => {
    await apiFetch(`/api/v1/api-keys/${id}`, { method: "DELETE" });
    setApiKeys((prev) => prev.filter((k) => k.id !== id));
  }, []);

  return { apiKeys, loading, error, createApiKey, deleteApiKey, refetch: fetchApiKeys };
}
