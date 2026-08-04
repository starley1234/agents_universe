"use client"
import { useEffect, useMemo, useState } from 'react'
import { Meter } from '../components/Meters'

type Task = { id: string; goal: string; status: string; current_state_json: any; budget_json: any; workspace_path: string; created_at: string; updated_at: string }
type Event = { id: string; event_type: string; payload_json: any; created_at: string }
const API = process.env.NEXT_PUBLIC_API_BASE || 'http://localhost:8128'

export default function Home() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [selected, setSelected] = useState<Task | null>(null)
  const [events, setEvents] = useState<Event[]>([])
  const [goal, setGoal] = useState('Prepare an autonomous research report about AetherMind architecture')
  async function load() { const r = await fetch(`${API}/api/tasks`); const d = await r.json(); setTasks(d); if (!selected && d[0]) setSelected(d[0]) }
  async function createTask() { await fetch(`${API}/api/tasks`, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({goal}) }); await load() }
  async function action(name: string) { if(!selected) return; await fetch(`${API}/api/tasks/${selected.id}/${name}`, { method: 'POST' }); await refreshSelected(selected.id) }
  async function refreshSelected(id: string) { const [t,e] = await Promise.all([fetch(`${API}/api/tasks/${id}`).then(r=>r.json()), fetch(`${API}/api/tasks/${id}/events`).then(r=>r.json())]); setSelected(t); setEvents(e); await load() }
  useEffect(()=>{ load() }, [])
  useEffect(()=>{ if(!selected) return; refreshSelected(selected.id); const timer=setInterval(()=>refreshSelected(selected.id), 2500); return ()=>clearInterval(timer) }, [selected?.id])
  const plan = selected?.current_state_json?.plan || []
  const confidence = (selected?.current_state_json?.confidence ?? 1) * 100
  const budget = selected?.budget_json || {}
  const iter = selected?.current_state_json?.iteration || 0
  const contextFill = useMemo(()=> Math.min(100, ((iter % 5) / 5) * 100), [iter])
  return <main className="min-h-screen bg-gradient-to-br from-[#070b16] to-[#111a33] p-6">
    <header className="mb-6 flex items-center justify-between"><div><h1 className="text-3xl font-bold text-white">AetherMind Mission Control</h1><p className="text-slate-400">Autonomous Iterative Engine</p></div><div className="rounded-full border border-neon/40 px-4 py-2 text-neon">{selected?.status || 'NO TASK'}</div></header>
    <section className="mb-6 grid gap-3 rounded-2xl border border-slate-800 bg-slate-950/70 p-4 md:grid-cols-[1fr_auto]"><input value={goal} onChange={e=>setGoal(e.target.value)} className="rounded-xl border border-slate-700 bg-slate-900 px-4 py-3 text-white outline-none focus:border-neon"/><button onClick={createTask} className="rounded-xl bg-neon px-5 py-3 font-semibold text-slate-950">Launch Agent</button></section>
    <div className="grid gap-6 lg:grid-cols-[260px_1fr_380px]">
      <aside className="rounded-2xl border border-slate-800 bg-slate-950/70 p-4"><h2 className="mb-3 font-semibold">Tasks</h2><div className="space-y-2">{tasks.map(t=><button key={t.id} onClick={()=>setSelected(t)} className={`block w-full rounded-xl p-3 text-left text-sm ${selected?.id===t.id?'bg-neon/20 text-neon':'bg-slate-900 text-slate-300'}`}><div className="truncate">{t.goal}</div><div className="text-xs opacity-70">{t.status}</div></button>)}</div></aside>
      <section className="space-y-6">
        <div className="rounded-2xl border border-slate-800 bg-slate-950/70 p-4"><h2 className="mb-4 font-semibold">Strategy Graph</h2><div className="grid gap-3 md:grid-cols-4">{plan.map((n:any)=><div key={n.id} className={`rounded-xl border p-4 ${n.status==='done'?'border-emerald-400 bg-emerald-400/10':n.status==='running'?'border-neon bg-neon/10':'border-slate-700 bg-slate-900'}`}><div className="text-xs uppercase text-slate-400">{n.status}</div><div className="font-medium">{n.title}</div></div>)}</div></div>
        <div className="rounded-2xl border border-slate-800 bg-slate-950/70 p-4"><h2 className="mb-4 font-semibold">Artifacts Canvas</h2><pre className="max-h-64 overflow-auto rounded-xl bg-slate-900 p-4 text-sm text-artifact">{JSON.stringify(selected?.current_state_json?.artifacts || [], null, 2)}</pre></div>
      </section>
      <aside className="space-y-6"><div className="rounded-2xl border border-slate-800 bg-slate-950/70 p-4"><h2 className="mb-4 font-semibold">Control & Guardrails</h2><div className="space-y-4"><Meter label="Confidence" value={confidence} color={confidence<50?'bg-red-500':'bg-emerald-400'} /><Meter label="Context heatmap" value={contextFill} color="bg-amberMind" /><Meter label="Iteration budget" value={(iter/(budget.max_iterations||25))*100} /><div className="grid grid-cols-2 gap-2"><button onClick={()=>action('pause')} className="rounded-lg bg-amberMind/20 px-3 py-2 text-amberMind">Pause</button><button onClick={()=>action('resume')} className="rounded-lg bg-emerald-400/20 px-3 py-2 text-emerald-300">Resume</button></div></div></div><div className="rounded-2xl border border-slate-800 bg-slate-950/70 p-4"><h2 className="mb-4 font-semibold">Live Trace</h2><div className="max-h-[520px] space-y-2 overflow-auto">{events.map(e=><div key={e.id} className="rounded-lg bg-slate-900 p-3 text-sm"><div className="text-xs text-neon">{e.event_type}</div><div className="text-slate-300">{e.payload_json?.message || JSON.stringify(e.payload_json)}</div></div>)}</div></div></aside>
    </div>
  </main>
}
