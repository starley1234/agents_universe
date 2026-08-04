export function Meter({ label, value, color='bg-neon' }: { label: string, value: number, color?: string }) {
  const pct = Math.max(0, Math.min(100, value))
  return <div className="space-y-1"><div className="flex justify-between text-xs text-slate-300"><span>{label}</span><span>{pct.toFixed(0)}%</span></div><div className="h-2 rounded bg-slate-800"><div className={`h-2 rounded ${color}`} style={{ width: `${pct}%` }} /></div></div>
}
