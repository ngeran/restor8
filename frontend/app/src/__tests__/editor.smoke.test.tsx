import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";

vi.mock("../api", () => ({
  api: {
    devices: vi.fn().mockResolvedValue([
      { id: 2, name: "ce2", mgmt_ip: "x", port: 830, platform: "CRPD", auth_ref: "lab-auth", containerlab_node: "ce2", created_at: "t" },
    ]),
    templates: vi.fn().mockResolvedValue([
      { group: "Routing", name: "static-route", description: "d", mode: "merge",
        fields: [
          { name: "destination", label: "Dest", type: "text" },
          { name: "action", label: "Action", type: "select", options: ["discard", "reject"] },
        ] },
    ]),
    render: vi.fn().mockResolvedValue({ payload: "set x y\n", mode: "merge" }),
    push: vi.fn().mockResolvedValue({ session_id: "s1", diff: "+ x", confirmed: true }),
    sessionStatus: vi.fn().mockResolvedValue({ session_id: "s1", host: "h", expires_in: 300 }),
    sessionConfirm: vi.fn().mockResolvedValue({}),
    sessionRollback: vi.fn().mockResolvedValue({ diff: "" }),
    running: vi.fn().mockResolvedValue({ device: "ce2", fmt: "set", config: "set version x" }),
    backups: vi.fn().mockResolvedValue([{ sha: "abc", date: "2026", message: "m" }]),
    diff: vi.fn().mockResolvedValue({ changed_lines: 1, diff: "+a\n-b" }),
    backupNow: vi.fn().mockResolvedValue({ device: "ce2", commit: "c", changed: true, path: "p" }),
    restore: vi.fn().mockResolvedValue({ restored: true, validation: { check: "config-match", passed: true } }),
  },
}));
vi.mock("../events", () => ({ useEvents: () => ({ events: [], state: { kind: "live" } }) }));
vi.mock("../resource", () => ({
  useResource: (key: string, fetcher: () => Promise<unknown>) => {
    // a REAL mini-implementation so screens actually populate
    const [data, setData] = React.useState<unknown>(undefined);
    React.useEffect(() => { fetcher().then(setData).catch(() => {}); }, [key]);
    return { data, error: undefined, loading: data === undefined, reload: () => { fetcher().then(setData).catch(() => {}); } };
  },
}));

import Configurations from "../Configurations";
import { ToastProvider } from "../toast";

const UI = () => <ToastProvider><Configurations /></ToastProvider>;

describe("Configurations editor (runtime smoke)", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("renders without crashing", () => {
    let view: ReturnType<typeof render> | null = null;
    expect(() => { view = render(<UI />); }).not.toThrow();
  });

  it("editor tab: dropdown + dynamic fields render", async () => {
    render(<UI />);
    await new Promise((r) => setTimeout(r, 50));
    fireEvent.click(screen.getAllByRole("button", { name: /^editor$/i })[0]);
    expect(screen.getByRole("combobox")).toBeTruthy();
    await screen.findByText(/static-route/);           // options loaded
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "static-route" } });
    expect(await screen.findByText(/Dest/i)).toBeTruthy();
    expect(screen.getByText(/Action/i)).toBeTruthy();
  });
});
