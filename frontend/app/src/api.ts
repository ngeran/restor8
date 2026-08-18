// restor8 API layer — everything goes through the gateway (same origin
// via the frontend nginx proxy / Ingress; vite dev proxy locally).

export interface Device {
  id: number;
  name: string;
  mgmt_ip: string;
  port: number;
  platform: string;
  auth_ref: string;
  containerlab_node: string | null;
  created_at: string;
}

export interface PlanNode { name: string; role: string; asn: number; loopback: string }
export interface PlanLink { a: string; a_if: string; a_ip: string; b: string; b_if: string; b_ip: string }
export interface Topology { name: string; underlay: string; nodes: PlanNode[]; links: PlanLink[] }

export interface Run {
  id: number;
  scenario: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  detail: unknown;
}

export interface BackupEntry { sha: string; date: string; message: string }

export interface TplField {
  name: string; label: string; type: string;
  default?: string | number | boolean; options?: string[]; placeholder?: string;
}
export interface Template {
  group: string; name: string; description: string; mode: string; fields: TplField[];
}
export interface Lab {
  group: string; name: string; description: string; steps: unknown[];
}

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status}: ${(await res.text()).slice(0, 200)}`);
  return res.json();
}
const post = <T>(url: string, body?: unknown) =>
  fetch(url, { method: "POST", headers: { "content-type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) }).then((r) => j<T>(r));

export const api = {
  devices: () => fetch("/api/devices").then((r) => j<Device[]>(r)),
  topology: () => fetch("/api/topology").then((r) => j<Topology>(r)),
  runs: () => fetch("/api/runs").then((r) => j<Run[]>(r)),
  run: (id: number) => fetch(`/api/runs/${id}`).then((r) => j<Run>(r)),
  backups: (id: number) => fetch(`/api/devices/${id}/backups`).then((r) => j<BackupEntry[]>(r)),
  backupNow: (id: number) => post<{ device: string; commit: string | null; changed: boolean; path: string }>(`/api/devices/${id}/backup`),
  restore: (id: number, sha: string) => post<Record<string, unknown>>(`/api/devices/${id}/restore/${sha}?approve=true`),
  diff: (id: number, sha: string) => fetch(`/api/devices/${id}/diff/${sha}`).then((r) => j<Record<string, unknown>>(r)),
  startRun: (name: string) => post<Record<string, unknown>>(`/api/scenarios/${name}/run`),
  renderScenario: (name: string) => post<{ scenario: string; underlay: string; targets: Record<string, string> }>(`/api/scenarios/${name}/render`),
  createDevice: (d: Record<string, unknown>) => post<Device>("/api/devices", d),
  updateDevice: (id: number, d: Record<string, unknown>) => fetch(`/api/devices/${id}`, { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify(d) }).then((r) => j<Device>(r)),
  deleteDevice: (id: number) => fetch(`/api/devices/${id}`, { method: "DELETE" }).then((r) => { if (!r.ok && r.status !== 204) throw new Error(`${r.status}`); return true; }),
  templates: () => fetch("/api/config/templates").then((r) => j<Template[]>(r)),
  render: (name: string, values: Record<string, unknown>) => post<{ payload: string; mode: string }>(`/api/config/templates/${name}/render`, { values }),
  running: (id: number, fmt = "set") => fetch(`/api/config/devices/${id}/running?fmt=${fmt}`).then((r) => j<{ device: string; fmt: string; config: string }>(r)),
  push: (body: Record<string, unknown>) => post<{ session_id: string; diff: string; confirmed: boolean }>("/api/config/push", body),
  labs: () => fetch("/api/labs").then((r) => j<Lab[]>(r)),
  applyLab: (name: string) => post<Record<string, unknown>>(`/api/labs/${name}/apply`),
  sessionStatus: (id: string) => fetch(`/api/session/${id}`).then((r) => j<{ session_id: string; host: string; expires_in: number }>(r)),
  sessionConfirm: (id: string) => post<{ session_id: string; action: string }>(`/api/session/${id}/confirm`),
  sessionRollback: (id: string) => post<{ session_id: string; action: string; diff: string }>(`/api/session/${id}/rollback`),
};
