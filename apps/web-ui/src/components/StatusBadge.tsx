const toneMap: Record<string, string> = {
  connected: "tone-positive",
  optimizable: "tone-positive",
  controllable: "tone-positive",
  discovered: "tone-neutral",
  visible_only: "tone-neutral",
  monitorable: "tone-neutral",
  partially_integrable: "tone-caution",
  protocol_incomplete: "tone-caution",
  in_analysis: "tone-caution",
  recovery_running: "tone-caution",
  blocked: "tone-critical",
  authentication_required: "tone-critical",
  manufacturer_access_required: "tone-critical",
  not_integratable: "tone-critical",
};

function humanize(value: string): string {
  return value.split("_").join(" ");
}

export function StatusBadge({ status }: { status: string }) {
  const tone = toneMap[status] ?? "tone-muted";

  return <span className={`status-badge ${tone}`}>{humanize(status)}</span>;
}
