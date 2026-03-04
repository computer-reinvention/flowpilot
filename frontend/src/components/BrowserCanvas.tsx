import { useEffect, useRef, useState, useCallback } from "react";
import {
  ArrowLeft,
  ArrowRight,
  RotateCw,
  Loader2,
  WifiOff,
  Globe,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { getToken } from "@/lib/api";

// Must match the browser viewport set by browser-use (BrowserSession sets 1920×1080).
// Frames arrive at this resolution from CDP (no maxWidth/maxHeight downscaling).
// Coordinates are then 1:1 with the browser viewport — no scaling math needed.
const CANVAS_W = 1920;
const CANVAS_H = 1080;

interface BrowserCanvasProps {
  sandboxId: string | null;
  /** Highlight the border when the agent is paused and user interaction is needed. */
  isPaused?: boolean;
  className?: string;
}

type ConnState = "idle" | "connecting" | "connected" | "reconnecting";

// ---------------------------------------------------------------------------
// Modifier bit flags (CDP convention)
// ---------------------------------------------------------------------------
function modifierBits(e: KeyboardEvent | React.KeyboardEvent | React.MouseEvent): number {
  return (
    (("altKey" in e && e.altKey) ? 1 : 0) |
    (("ctrlKey" in e && e.ctrlKey) ? 2 : 0) |
    (("metaKey" in e && e.metaKey) ? 4 : 0) |
    (("shiftKey" in e && e.shiftKey) ? 8 : 0)
  );
}

// Map browser button index → CDP button name
const BTN_MAP: Record<number, string> = { 0: "left", 1: "middle", 2: "right" };

export function BrowserCanvas({ sandboxId, isPaused, className }: BrowserCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectDelay = useRef(1000);
  const mountedRef = useRef(true);

  const [connState, setConnState] = useState<ConnState>("idle");
  const [currentUrl, setCurrentUrl] = useState<string>("");
  const [isLoading, setIsLoading] = useState(false);
  const [urlInput, setUrlInput] = useState("");
  const [isEditingUrl, setIsEditingUrl] = useState(false);

  // ---------------------------------------------------------------------------
  // WebSocket connection
  // ---------------------------------------------------------------------------
  const connect = useCallback(
    (id: string) => {
      if (!mountedRef.current) return;

      // Close any existing connection
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
        wsRef.current = null;
      }

      setConnState("connecting");

      const protocol = location.protocol === "https:" ? "wss:" : "ws:";
      const token = getToken() ?? "";
      const ws = new WebSocket(
        `${protocol}//${location.host}/ws/browser/${id}?token=${encodeURIComponent(token)}`,
      );
      wsRef.current = ws;

      ws.onopen = () => {
        if (!mountedRef.current) return;
        reconnectDelay.current = 1000; // reset backoff on successful connect
        setConnState("connected");
      };

      ws.onmessage = (evt) => {
        if (!mountedRef.current) return;
        try {
          const msg = JSON.parse(evt.data as string) as {
            type: string;
            data?: string;
            sessionId?: number;
            url?: string;
            loading?: boolean;
          };

          if (msg.type === "frame" && msg.data) {
            renderFrame(msg.data);
          } else if (msg.type === "nav" && msg.url) {
            setCurrentUrl(msg.url);
            if (!isEditingUrl) setUrlInput(msg.url);
          } else if (msg.type === "loading") {
            setIsLoading(!!msg.loading);
          }
        } catch {
          // ignore malformed messages
        }
      };

      ws.onclose = () => {
        if (!mountedRef.current) return;
        setConnState("reconnecting");
        // Exponential backoff capped at 8 s
        const delay = reconnectDelay.current;
        reconnectDelay.current = Math.min(delay * 2, 8000);
        reconnectTimer.current = setTimeout(() => connect(id), delay);
      };

      ws.onerror = () => {
        // onclose fires right after, which handles reconnect
      };
    },
    [], // stable — no deps change at runtime
  );

  useEffect(() => {
    mountedRef.current = true;
    if (sandboxId) {
      connect(sandboxId);
    } else {
      setConnState("idle");
    }
    return () => {
      mountedRef.current = false;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      if (wsRef.current) {
        wsRef.current.onclose = null; // prevent reconnect on unmount
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [sandboxId, connect]);

  // ---------------------------------------------------------------------------
  // Frame rendering
  // ---------------------------------------------------------------------------
  function renderFrame(base64: string) {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // Convert base64 → Blob → ImageBitmap (GPU-decoded, non-blocking)
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const blob = new Blob([bytes], { type: "image/jpeg" });

    createImageBitmap(blob).then((bitmap) => {
      ctx.drawImage(bitmap, 0, 0);
      bitmap.close();
    });
  }

  // ---------------------------------------------------------------------------
  // Coordinate scaling
  // ---------------------------------------------------------------------------
  function canvasCoords(e: React.MouseEvent<HTMLCanvasElement>) {
    const rect = e.currentTarget.getBoundingClientRect();
    return {
      x: Math.round(((e.clientX - rect.left) / rect.width) * CANVAS_W),
      y: Math.round(((e.clientY - rect.top) / rect.height) * CANVAS_H),
    };
  }

  // ---------------------------------------------------------------------------
  // Input helpers
  // ---------------------------------------------------------------------------
  function send(data: object) {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(data));
    }
  }

  // ---------------------------------------------------------------------------
  // Navigation helpers
  // ---------------------------------------------------------------------------
  function navigateBack() {
    send({ type: "navigate", action: "back" });
  }
  function navigateForward() {
    send({ type: "navigate", action: "forward" });
  }
  function navigateReload() {
    send({ type: "navigate", action: "reload" });
  }
  function navigateToUrl(url: string) {
    send({ type: "navigate", action: "goto", url });
  }

  // Register wheel as a native non-passive listener so preventDefault() actually works.
  // React's onWheel is passive by default — e.preventDefault() is silently ignored there.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      send({
        type: "mouse",
        eventType: "mouseWheel",
        x: Math.round(((e.clientX - rect.left) / rect.width) * CANVAS_W),
        y: Math.round(((e.clientY - rect.top) / rect.height) * CANVAS_H),
        button: "none",
        buttons: 0,
        clickCount: 0,
        modifiers: (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0),
        deltaX: e.deltaX,
        deltaY: e.deltaY,
      });
    };
    canvas.addEventListener("wheel", onWheel, { passive: false });
    return () => canvas.removeEventListener("wheel", onWheel);
  }, []); // canvasRef is stable

  // Throttle for mousemove events (both hover and drag).
  const lastMoveRef = useRef(0);

  // ---------------------------------------------------------------------------
  // Mouse handlers
  // ---------------------------------------------------------------------------
  const handleMouseDown = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    e.preventDefault();
    // preventDefault suppresses default focus transfer — restore it manually.
    e.currentTarget.focus();
    const { x, y } = canvasCoords(e);
    const mods = modifierBits(e);
    // Send mouseMoved first — ensures the browser fires mouseover/mouseenter
    // on the target element before the click, matching real user behaviour.
    // (This is what Puppeteer/Playwright do internally before every click.)
    send({
      type: "mouse",
      eventType: "mouseMoved",
      x, y,
      button: "none",
      buttons: 0,
      clickCount: 0,
      modifiers: mods,
    });
    send({
      type: "mouse",
      eventType: "mousePressed",
      x, y,
      button: BTN_MAP[e.button] ?? "left",
      buttons: e.buttons,
      clickCount: 1,
      modifiers: mods,
    });
  }, []);

  const handleMouseUp = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    e.preventDefault();
    const { x, y } = canvasCoords(e);
    send({
      type: "mouse",
      eventType: "mouseReleased",
      x, y,
      button: BTN_MAP[e.button] ?? "left",
      buttons: e.buttons,
      clickCount: 1,
      modifiers: modifierBits(e),
    });
  }, []);

  // Forward mousemove for both hover and drag.
  // Hover moves are essential — without them the browser never fires
  // mouseover/mouseenter events and :hover CSS states don't apply.
  // Throttle: ~16 fps (60 ms) for idle hover, ~30 fps (33 ms) for drag.
  const handleMouseMove = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const now = Date.now();
    const throttle = e.buttons === 0 ? 60 : 33;
    if (now - lastMoveRef.current < throttle) return;
    lastMoveRef.current = now;
    const { x, y } = canvasCoords(e);
    send({
      type: "mouse",
      eventType: "mouseMoved",
      x, y,
      button: "none",
      buttons: e.buttons,
      clickCount: 0,
      modifiers: modifierBits(e),
    });
  }, []);

  // ---------------------------------------------------------------------------
  // Keyboard handlers
  // ---------------------------------------------------------------------------
  const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLCanvasElement>) => {
    // Let Ctrl/Meta+R pass through (browser reload) but swallow everything else
    // that would interfere with typing into the remote browser.
    if ((e.ctrlKey || e.metaKey) && ["r", "w", "t", "l"].includes(e.key.toLowerCase())) {
      return;
    }
    e.preventDefault();

    const mods = modifierBits(e);
    // unmodifiedText: what the key produces without shift (e.g. 'a' even if shift+'A')
    const unmodified = e.key.length === 1 ? e.key.toLowerCase() : "";
    // location: 0=standard, 1=left, 2=right (distinguishes Left/Right Ctrl, Shift, Alt)
    const loc = e.location ?? 0;

    send({
      type: "key",
      eventType: "keyDown",
      key: e.key,
      code: e.code,
      text: "",
      unmodifiedText: unmodified,
      modifiers: mods,
      keyCode: e.keyCode,
      location: loc,
      isKeypad: loc === 3,
    });

    // For printable characters (single char, no Ctrl/Meta), also send a char event
    // so Chromium inserts the character into focused inputs.
    if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) {
      send({
        type: "key",
        eventType: "char",
        key: e.key,
        code: e.code,
        text: e.key,
        unmodifiedText: unmodified,
        modifiers: mods,
        keyCode: e.keyCode,
        location: loc,
      });
    }
  }, []);

  const handleKeyUp = useCallback((e: React.KeyboardEvent<HTMLCanvasElement>) => {
    e.preventDefault();
    send({
      type: "key",
      eventType: "keyUp",
      key: e.key,
      code: e.code,
      text: "",
      unmodifiedText: e.key.length === 1 ? e.key.toLowerCase() : "",
      modifiers: modifierBits(e),
      keyCode: e.keyCode,
      location: e.location ?? 0,
    });
  }, []);

  // ---------------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------------
  const isConnecting = connState === "connecting" || connState === "reconnecting";
  const isIdle = connState === "idle" || !sandboxId;
  const showToolbar = connState === "connected";

  return (
    <div className={cn("flex flex-col h-full w-full bg-black overflow-hidden select-none", className)}>
      {/* Browser toolbar */}
      {showToolbar && (
        <div className="flex items-center gap-1 px-2 py-1.5 bg-zinc-900 border-b border-zinc-800 shrink-0">
          {/* Nav buttons */}
          <button
            onClick={navigateBack}
            className="p-1 rounded hover:bg-zinc-700 text-zinc-400 hover:text-zinc-200 transition-colors"
            title="Back"
          >
            <ArrowLeft className="size-3.5" />
          </button>
          <button
            onClick={navigateForward}
            className="p-1 rounded hover:bg-zinc-700 text-zinc-400 hover:text-zinc-200 transition-colors"
            title="Forward"
          >
            <ArrowRight className="size-3.5" />
          </button>
          <button
            onClick={navigateReload}
            className="p-1 rounded hover:bg-zinc-700 text-zinc-400 hover:text-zinc-200 transition-colors"
            title="Reload"
          >
            {isLoading ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <RotateCw className="size-3.5" />
            )}
          </button>

          {/* URL bar */}
          <div className="flex-1 flex items-center gap-1.5 bg-zinc-800 rounded px-2 py-0.5 ml-1 min-w-0">
            <Globe className="size-3 text-zinc-500 shrink-0" />
            <input
              type="text"
              value={isEditingUrl ? urlInput : currentUrl}
              onChange={(e) => setUrlInput(e.target.value)}
              onFocus={() => {
                setIsEditingUrl(true);
                setUrlInput(currentUrl);
              }}
              onBlur={() => setIsEditingUrl(false)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  navigateToUrl(urlInput);
                  setIsEditingUrl(false);
                  (e.target as HTMLInputElement).blur();
                } else if (e.key === "Escape") {
                  setUrlInput(currentUrl);
                  setIsEditingUrl(false);
                  (e.target as HTMLInputElement).blur();
                }
              }}
              className="flex-1 bg-transparent text-[11px] text-zinc-300 font-mono outline-none placeholder:text-zinc-600 min-w-0"
              placeholder="Enter URL..."
              spellCheck={false}
            />
          </div>

          {/* Loading bar */}
          {isLoading && (
            <div className="absolute bottom-0 left-0 right-0 h-[2px] bg-zinc-800 overflow-hidden">
              <div className="h-full w-1/3 bg-blue-500 animate-[slide_1s_ease-in-out_infinite]" />
            </div>
          )}
        </div>
      )}

      {/* Canvas area */}
      <div className="relative flex-1 flex items-center justify-center overflow-hidden">
        <div
          className="relative"
          style={{ aspectRatio: `${CANVAS_W} / ${CANVAS_H}`, maxWidth: "100%", maxHeight: "100%" }}
        >
          <canvas
            ref={canvasRef}
            width={CANVAS_W}
            height={CANVAS_H}
            className={cn(
              "w-full h-full block focus:outline-none cursor-default",
              isPaused && "ring-2 ring-amber-400 ring-inset",
            )}
            tabIndex={0}
            onMouseDown={handleMouseDown}
            onMouseUp={handleMouseUp}
            onMouseMove={handleMouseMove}
            onKeyDown={handleKeyDown}
            onKeyUp={handleKeyUp}
            onContextMenu={(e) => e.preventDefault()}
          />

          {/* Overlays — inside the wrapper so they cover only the canvas, not the black bars */}
          {isIdle && (
            <div className="absolute inset-0 flex items-center justify-center text-muted-foreground text-sm">
              Waiting for browser...
            </div>
          )}

          {isConnecting && (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-black/60">
              {connState === "reconnecting" ? (
                <>
                  <WifiOff className="size-5 text-muted-foreground" />
                  <span className="text-sm text-muted-foreground">Reconnecting...</span>
                </>
              ) : (
                <>
                  <Loader2 className="size-6 animate-spin text-muted-foreground" />
                  <span className="text-sm text-muted-foreground">Connecting to browser...</span>
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
