export function CapabilityPill({ label, enabled }: { label: string; enabled: boolean }) {
  return <span className={`capability-pill ${enabled ? "enabled" : "disabled"}`}>{label}</span>;
}

