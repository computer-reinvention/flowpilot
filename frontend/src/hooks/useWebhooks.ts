import { useState, useEffect, useCallback } from "react";
import type { Webhook } from "@/lib/types";
import { apiFetch } from "@/lib/api";

export function useWebhooks() {
  const [webhooks, setWebhooks] = useState<Webhook[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchWebhooks = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch<Webhook[]>("/api/v1/webhooks");
      setWebhooks(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch webhooks");
      setWebhooks([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchWebhooks();
  }, [fetchWebhooks]);

  const createWebhook = useCallback(async (url: string, events: string[]): Promise<Webhook> => {
    const data = await apiFetch<Webhook>("/api/v1/webhooks", {
      method: "POST",
      body: JSON.stringify({ url, events }),
    });
    setWebhooks((prev) => [data, ...prev]);
    return data;
  }, []);

  const deleteWebhook = useCallback(async (id: string) => {
    await apiFetch(`/api/v1/webhooks/${id}`, { method: "DELETE" });
    setWebhooks((prev) => prev.filter((w) => w.id !== id));
  }, []);

  const testWebhook = useCallback(async (id: string): Promise<{ status: string; response_status?: number; error?: string }> => {
    return await apiFetch(`/api/v1/webhooks/${id}/test`, { method: "POST" });
  }, []);

  return { webhooks, loading, error, createWebhook, deleteWebhook, testWebhook, refetch: fetchWebhooks };
}
