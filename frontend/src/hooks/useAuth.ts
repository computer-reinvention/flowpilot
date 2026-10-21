import { useState, useEffect, useCallback } from "react";
import type { User, AuthResponse } from "@/lib/types";
import { apiFetch, getToken, setToken, clearToken } from "@/lib/api";

export function useAuth() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const verify = useCallback(async () => {
    const token = getToken();
    if (!token) {
      setLoading(false);
      return;
    }
    try {
      const data = await apiFetch<{ user: User }>("/api/auth/me");
      if (getToken() === token) setUser(data.user);
    } catch (err) {
      if (getToken() === token && !(err instanceof DOMException && err.name === "AbortError")) clearToken();
    } finally {
      if (getToken() === token || !getToken()) setLoading(false);
    }
  }, []);

  useEffect(() => {
    verify();
    const changed = () => { setUser(null); setError(null); setLoading(true); void verify(); };
    const storage = (event: StorageEvent) => { if (event.key === "flowpilot_token" || event.key === null) changed(); };
    window.addEventListener("flowpilot-auth-changed", changed);
    window.addEventListener("storage", storage);
    return () => { window.removeEventListener("flowpilot-auth-changed", changed); window.removeEventListener("storage", storage); };
  }, [verify]);

  const login = useCallback(async (email: string, password: string) => {
    setError(null);
    try {
      const data = await apiFetch<AuthResponse>("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      setToken(data.token);
      setUser(data.user);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Login failed";
      setError(msg);
      throw err;
    }
  }, []);

  const signup = useCallback(
    async (email: string, password: string, name: string) => {
      setError(null);
      try {
        const data = await apiFetch<AuthResponse>("/api/auth/signup", {
          method: "POST",
          body: JSON.stringify({ email, password, name }),
        });
        setToken(data.token);
        setUser(data.user);
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Signup failed";
        setError(msg);
        throw err;
      }
    },
    [],
  );

  const logout = useCallback(() => {
    clearToken();
    setUser(null);
  }, []);

  return { user, loading, error, login, signup, logout };
}
