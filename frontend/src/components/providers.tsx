"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, type RunListItem } from "@/lib/api";
import { storage } from "@/lib/storage";

interface AppState {
  runs: RunListItem[];
  refreshRuns: () => Promise<void>;
  reviewer: string;
  setReviewer: (name: string) => void;
  toast: (message: string) => void;
}

const AppContext = createContext<AppState | null>(null);

export function useApp(): AppState {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp must be used inside <Providers>");
  return ctx;
}

export function Providers({ children }: { children: ReactNode }) {
  const [runs, setRuns] = useState<RunListItem[]>([]);
  const [reviewer, setReviewerState] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const refreshRuns = useCallback(async () => {
    try {
      setRuns(await api.runs());
    } catch {
      /* backend down: the sidebar just stays empty */
    }
  }, []);

  const toast = useCallback((msg: string) => {
    setMessage(msg);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setMessage(null), 3500);
  }, []);

  const setReviewer = useCallback((name: string) => {
    setReviewerState(name);
    storage.set("reviewer", name);
  }, []);

  useEffect(() => {
    setReviewerState(storage.get("reviewer", ""));
    void refreshRuns();
  }, [refreshRuns]);

  return (
    <AppContext.Provider value={{ runs, refreshRuns, reviewer, setReviewer, toast }}>
      {children}
      <div className={`toast${message ? " show" : ""}`} role="status" aria-live="polite">
        {message}
      </div>
    </AppContext.Provider>
  );
}
