let permissionGranted = false;
let titleInterval: ReturnType<typeof setInterval> | null = null;
const originalTitle = document.title;

export async function requestNotificationPermission(): Promise<boolean> {
  if (!("Notification" in window)) return false;
  if (Notification.permission === "granted") {
    permissionGranted = true;
    return true;
  }
  if (Notification.permission === "denied") return false;
  const result = await Notification.requestPermission();
  permissionGranted = result === "granted";
  return permissionGranted;
}

export function sendHITLNotification(reason: string) {
  // Layer 1: Browser notification
  if (permissionGranted && "Notification" in window) {
    new Notification("FlowPilot: Login Required", {
      body: reason,
      icon: "/favicon.ico",
      requireInteraction: true,
    });
  }

  // Layer 2: Audio alert
  try {
    const audio = new Audio("/alert.wav");
    audio.volume = 0.5;
    audio.play().catch(() => {});
  } catch {
    // audio not available
  }

  // Layer 3: Title flash
  startTitleFlash();
}

let visibilityHandler: (() => void) | null = null;

export function startTitleFlash() {
  if (titleInterval) return;
  let toggle = false;

  const tick = () => {
    document.title = toggle
      ? originalTitle
      : "\u26A0\uFE0F LOGIN REQUIRED \u2014 FlowPilot";
    toggle = !toggle;
  };

  titleInterval = setInterval(tick, 1000);

  // Pause looping animation when page is not visible
  visibilityHandler = () => {
    if (document.hidden) {
      if (titleInterval) {
        clearInterval(titleInterval);
        titleInterval = null;
      }
    } else {
      if (!titleInterval) {
        titleInterval = setInterval(tick, 1000);
      }
    }
  };
  document.addEventListener("visibilitychange", visibilityHandler);
}

export function stopTitleFlash() {
  if (titleInterval) {
    clearInterval(titleInterval);
    titleInterval = null;
  }
  if (visibilityHandler) {
    document.removeEventListener("visibilitychange", visibilityHandler);
    visibilityHandler = null;
  }
  document.title = originalTitle;
}
