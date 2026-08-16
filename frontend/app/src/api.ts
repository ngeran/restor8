// restor8 API layer — everything goes through the gateway (same origin
// via Ingress in-cluster, via the vite dev proxy locally).

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

export interface PlanNode {
  name: string;
  role: string;
  asn: number;
  loopback: string;
}
export interface PlanLink {
  a: string;
  a_if: string;
  a_ip: string;
  b: string;
  b_if: string;
  b_ip: string;
}
export interface Topology {
  name: string;
  underlay: string;
  nodes: PlanNode[];
  links: PlanLink[];
}

export interface Run {
  id: number;
  scenario: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  detail: unknown;
}

export interface BackupEntry {
  sha: string;
  date: string;
  message: string;
}

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`.slice(0, 200));
  return res.json();
}

export const api = {
  devices: () => fetch("/api/devices").then((r) => j<Device[]>(r)),
  topology: () => fetch("/api/topology").then((r) => j<Topology>(r)),
  runs: () => fetch("/api/runs").then((r) => j<Run[]>(r)),
  run: (id: number) => fetch(`/api/runs/${id}`).then((r) => j<Run>(r)),
  backups: (id: number) =>
    fetch(`/api/devices/${id}/backups`).then((r) => j<BackupEntry[]>(r)),
  diff: (id: number, sha: string) =>
    fetch(`/api/devices/${id}/diff/${sha}`).then((r) => j<Record<string, unknown>>(r)),
  startRun: (name: string) =>
    fetch(`/api/scenarios/${name}/run`, { method: "POST" }).then((r) => j<Record<string, unknown>>(r)),
};
