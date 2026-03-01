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
      setUser(data.user);
    } catch {
      clearToken();
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    verify();
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
