export function CapabilityPill({ label, enabled }: { label: string; enabled: boolean }) {
  return (
    <span
      className={`inline-flex items-center rounded-md border px-2 py-1 text-[11px] font-medium ${
        enabled ? "border-[#f0d8aa] bg-[#fff6e6] text-[#a56614]" : "border-slate-200 bg-slate-100 text-slate-500"
      }`}
    >
      {label}
    </span>
  );
}
