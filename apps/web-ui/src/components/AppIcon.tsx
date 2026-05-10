import type { SVGProps } from "react";

export type AppIconName =
  | "overview"
  | "devices"
  | "monitoring"
  | "control"
  | "settings"
  | "assistant"
  | "sun"
  | "send"
  | "home"
  | "discover"
  | "grid"
  | "pv"
  | "battery"
  | "ev"
  | "loads"
  | "hvac"
  | "refresh"
  | "lock"
  | "unlock"
  | "chevron-left"
  | "chevron-right"
  | "link"
  | "trash"
  | "x";

type AppIconProps = SVGProps<SVGSVGElement> & {
  name: AppIconName;
};

export function AppIcon({ name, className, ...props }: AppIconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      {...props}
    >
      {name === "overview" ? (
        <>
          <rect x="4" y="4" width="6" height="6" rx="1.2" />
          <rect x="14" y="4" width="6" height="6" rx="1.2" />
          <rect x="4" y="14" width="6" height="6" rx="1.2" />
          <rect x="14" y="14" width="6" height="6" rx="1.2" />
        </>
      ) : null}
      {name === "devices" ? (
        <>
          <path d="M9 8V5m6 3V5" />
          <rect x="6.5" y="8" width="11" height="9" rx="2" />
          <path d="M10 17v2m4-2v2m-6 0h8" />
        </>
      ) : null}
      {name === "monitoring" ? (
        <>
          <path d="M4 17V7" />
          <path d="M10 17V11" />
          <path d="M16 17V4" />
          <path d="M22 17V9" />
        </>
      ) : null}
      {name === "control" ? (
        <>
          <path d="M6 6h12" />
          <path d="M6 12h12" />
          <path d="M6 18h12" />
          <circle cx="10" cy="6" r="2" />
          <circle cx="15" cy="12" r="2" />
          <circle cx="8" cy="18" r="2" />
        </>
      ) : null}
      {name === "settings" ? (
        <>
          <circle cx="12" cy="12" r="3.2" />
          <path d="M19.4 15a1 1 0 0 0 .2 1.1l.1.1a2 2 0 0 1-2.8 2.8l-.1-.1a1 1 0 0 0-1.1-.2 1 1 0 0 0-.6.9V20a2 2 0 0 1-4 0v-.2a1 1 0 0 0-.6-.9 1 1 0 0 0-1.1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1 1 0 0 0 .2-1.1 1 1 0 0 0-.9-.6H4a2 2 0 0 1 0-4h.2a1 1 0 0 0 .9-.6 1 1 0 0 0-.2-1.1l-.1-.1a2 2 0 0 1 2.8-2.8l.1.1a1 1 0 0 0 1.1.2 1 1 0 0 0 .6-.9V4a2 2 0 0 1 4 0v.2a1 1 0 0 0 .6.9 1 1 0 0 0 1.1-.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1 1 0 0 0-.2 1.1 1 1 0 0 0 .9.6H20a2 2 0 0 1 0 4h-.2a1 1 0 0 0-.9.6Z" />
        </>
      ) : null}
      {name === "assistant" ? (
        <>
          <path d="M8 9h8" />
          <path d="M8 13h5" />
          <path d="M7 5h10a3 3 0 0 1 3 3v6a3 3 0 0 1-3 3h-6l-4 3v-3H7a3 3 0 0 1-3-3V8a3 3 0 0 1 3-3Z" />
        </>
      ) : null}
      {name === "sun" ? (
        <>
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2.5v2.3" />
          <path d="M12 19.2v2.3" />
          <path d="m4.9 4.9 1.6 1.6" />
          <path d="m17.5 17.5 1.6 1.6" />
          <path d="M2.5 12h2.3" />
          <path d="M19.2 12h2.3" />
          <path d="m4.9 19.1 1.6-1.6" />
          <path d="m17.5 6.5 1.6-1.6" />
        </>
      ) : null}
      {name === "send" ? (
        <>
          <path d="M21 3 10 14" />
          <path d="m21 3-7 18-4-7-7-4 18-7Z" />
        </>
      ) : null}
      {name === "home" ? (
        <>
          <path d="M4 11.5 12 5l8 6.5" />
          <path d="M7 10.5V19h10v-8.5" />
          <path d="M10 19v-5h4v5" />
        </>
      ) : null}
      {name === "discover" ? (
        <>
          <circle cx="12" cy="12" r="1.8" />
          <path d="M12 12 18 6" />
          <path d="M5.6 18.4a9 9 0 1 1 12.8 0" />
          <path d="M8.4 15.6a5 5 0 1 1 7.2 0" />
          <path d="M3 21h18" />
        </>
      ) : null}
      {name === "grid" ? (
        <>
          <path d="M7 20h10" />
          <path d="M12 4v16" />
          <path d="M8 8h8" />
          <path d="M7 12h10" />
          <path d="M6 16h12" />
        </>
      ) : null}
      {name === "pv" ? (
        <>
          <path d="M5 15h14" />
          <path d="M7 15 9 8h6l2 7" />
          <path d="M12 5V3" />
          <path d="M6 5.5 4.5 4" />
          <path d="M18 5.5 19.5 4" />
        </>
      ) : null}
      {name === "battery" ? (
        <>
          <rect x="5" y="7" width="13" height="10" rx="2" />
          <path d="M18 10h1.5a1 1 0 0 1 1 1v2a1 1 0 0 1-1 1H18" />
          <path d="M9 10v4" />
          <path d="M12 10v4" />
          <path d="M15 10v4" />
        </>
      ) : null}
      {name === "ev" ? (
        <>
          <path d="M7 8h7l3 4v5H7a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2Z" />
          <path d="M16 8V6" />
          <circle cx="9" cy="17" r="1.5" />
          <circle cx="15" cy="17" r="1.5" />
        </>
      ) : null}
      {name === "loads" ? (
        <>
          <path d="M10 3 6 13h5l-1 8 8-12h-5l2-6Z" />
        </>
      ) : null}
      {name === "hvac" ? (
        <>
          <path d="M12 3v18" />
          <path d="m16.5 6-9 5.2" />
          <path d="m16.5 18-9-5.2" />
        </>
      ) : null}
      {name === "refresh" ? (
        <>
          <path d="M20 11a8 8 0 1 0 2 5.3" />
          <path d="M20 5v6h-6" />
        </>
      ) : null}
      {name === "lock" ? (
        <>
          <rect x="5" y="11" width="14" height="10" rx="2" />
          <path d="M8 11V8a4 4 0 1 1 8 0v3" />
        </>
      ) : null}
      {name === "unlock" ? (
        <>
          <rect x="5" y="11" width="14" height="10" rx="2" />
          <path d="M8 11V8a4 4 0 0 1 7-2.5" />
        </>
      ) : null}
      {name === "chevron-left" ? <path d="m15 18-6-6 6-6" /> : null}
      {name === "chevron-right" ? <path d="m9 18 6-6-6-6" /> : null}
      {name === "link" ? (
        <>
          <path d="M10 13a5 5 0 0 0 7.1 0l2-2a5 5 0 0 0-7.1-7.1l-1.1 1.1" />
          <path d="M14 11a5 5 0 0 0-7.1 0l-2 2a5 5 0 1 0 7.1 7.1l1.1-1.1" />
        </>
      ) : null}
      {name === "trash" ? (
        <>
          <path d="M4 7h16" />
          <path d="M10 11v6" />
          <path d="M14 11v6" />
          <path d="M6 7l1 14h10l1-14" />
          <path d="M9 7V4h6v3" />
        </>
      ) : null}
      {name === "x" ? (
        <>
          <path d="M18 6 6 18" />
          <path d="m6 6 12 12" />
        </>
      ) : null}
    </svg>
  );
}
