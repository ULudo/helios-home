const toneMap: Record<string, string> = {
  connected: "border-emerald-200 bg-emerald-50 text-emerald-700",
  optimizable: "border-emerald-200 bg-emerald-50 text-emerald-700",
  controllable: "border-emerald-200 bg-emerald-50 text-emerald-700",
  discovered: "border-slate-200 bg-slate-50 text-slate-700",
  visible_only: "border-slate-200 bg-slate-50 text-slate-700",
  monitorable: "border-slate-200 bg-slate-50 text-slate-700",
  partially_integrable: "border-amber-200 bg-amber-50 text-amber-700",
  protocol_incomplete: "border-amber-200 bg-amber-50 text-amber-700",
  in_analysis: "border-amber-200 bg-amber-50 text-amber-700",
  recovery_running: "border-amber-200 bg-amber-50 text-amber-700",
  blocked: "border-rose-200 bg-rose-50 text-rose-700",
  authentication_required: "border-rose-200 bg-rose-50 text-rose-700",
  manufacturer_access_required: "border-rose-200 bg-rose-50 text-rose-700",
  not_integratable: "border-rose-200 bg-rose-50 text-rose-700",
};

function humanize(value: string): string {
  return value.split("_").join(" ");
}

export function StatusBadge({ status }: { status: string }) {
  const tone = toneMap[status] ?? "border-slate-200 bg-slate-100 text-slate-600";

  return (
    <span className={`inline-flex items-center rounded-md border px-2 py-1 text-[11px] font-semibold tracking-[0.08em] uppercase ${tone}`}>
      {humanize(status)}
    </span>
  );
}
