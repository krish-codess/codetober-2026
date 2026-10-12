import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { DarkEvent } from "./api";
import { Investigation } from "./Investigation";
import { LiveMap } from "./LiveMap";

// jsdom has no WebGL. The map canvas is replaced; everything a keyboard or screen-reader user touches is real.
vi.mock("./useDeck", async (original) => ({ ...(await original<typeof import("./useDeck")>()), useDeck: () => ({ current: null }) }));

const event = (over: Partial<DarkEvent> = {}): DarkEvent => ({
  id: 1, mmsi: 477001198, name: "STAR QUEEN", flag: "HK", classification: "dark", score: 100, impossible_speed: true,
  started_at: "2026-10-09T10:01:00Z", ended_at: "2026-10-09T10:47:00Z", duration_s: 2760, start: [55.2, 25.7], end: [151.7, -34.1],
  distance_nm: 6552.4, implied_speed_kn: 8546.6, start_reliability: 1, end_reliability: 1, neighbours_heard: 12,
  reasons: ["silent for 46 min", "12 other vessels heard nearby during the silence", "reappeared 6552 nm away: needs 8547 kn, limit 50 kn"], ...over,
}); // prettier-ignore
const LIVE = { server_time: "2026-10-09T18:00:05Z", data_time: new Date().toISOString(), truncated: false, vessels: [[257000001, 5, 60, 10, 90, null, 1791568800]] };
const VESSEL = { mmsi: 477001198, name: "STAR QUEEN", callsign: "VRAB1", imo: null, ship_type: 70, length_m: 200, beam_m: 32, flag: "HK", country: "Hong Kong",
  first_seen: "2026-10-09T06:00:00Z", last_seen: "2026-10-09T17:59:00Z", lon: 151.7, lat: -34.1, sog: 12, events: 1 }; // prettier-ignore
// 2026-10-09 10:00:00, 10:01:00 and 10:50:00 UTC
const TRACK = { mmsi: 477001198, points: [[1791540000, 55.1, 25.6, 12, 0], [1791540060, 55.2, 25.7, 12, 0], [1791543000, 151.7, -34.1, 12, 0]], next_cursor: null }; // prettier-ignore

type Reply = unknown | (() => unknown);
const fail = (status: number, message: string) => () => new Response(JSON.stringify({ error: { code: "x", message, details: [], request_id: "req-1" } }), { status });
let calls: string[] = [];

/** Answer fetch by the first route whose key appears in the URL. A function reply can vary per call. */
function serve(routes: Record<string, Reply>) {
  calls = [];
  vi.stubGlobal("fetch", async (url: string) => {
    calls.push(url);
    const key = Object.keys(routes).find((k) => url.includes(k));
    if (!key) throw new TypeError("network down");
    const reply = routes[key];
    const value = typeof reply === "function" ? (reply as () => unknown)() : reply;
    return value instanceof Response ? value : new Response(JSON.stringify(value), { status: 200 });
  });
}

function show(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 10_000 } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

afterEach(() => vi.unstubAllGlobals());

describe("live map panel", () => {
  it("shows loading, then the fleet and the ranked events with their class in words", async () => {
    const ongoing = event({ id: 2, name: null, mmsi: 257000009, classification: "coverage_gap", score: 5, impossible_speed: false, ended_at: null });
    serve({ "/vessels/live": LIVE, "/dark-events": { items: [event(), ongoing] } });
    show(<LiveMap onOpenVessel={() => {}} />);
    expect(screen.getByText("Loading the fleet…")).toBeInTheDocument();
    expect(screen.getByText("Loading ranked events…")).toBeInTheDocument();
    expect(await screen.findByText(/vessels tracked/)).toHaveTextContent("1 vessels tracked");
    const first = await screen.findByRole("button", { name: /STAR QUEEN/ });
    expect(first).toHaveTextContent("Went dark, impossible speed · 46 min");
    expect(within(first).getByLabelText("score 100")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /MMSI 257000009/ })).toHaveTextContent("Coverage gap · 46 min, ongoing");
  });

  it("selecting an event by keyboard reveals the evidence and leads to the investigation", async () => {
    const open = vi.fn();
    serve({ "/vessels/live": LIVE, "/dark-events": { items: [event()] } });
    show(<LiveMap onOpenVessel={open} />);
    const row = await screen.findByRole("button", { name: /STAR QUEEN/ });
    row.focus();
    await userEvent.keyboard("{Enter}");
    expect(row).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("reappeared 6552 nm away: needs 8547 kn, limit 50 kn")).toBeInTheDocument();
    await userEvent.tab();
    await userEvent.keyboard("{Enter}");
    expect(open).toHaveBeenCalledWith(477001198);
  });

  it("says so when nothing matches, and refetches with the chosen filter", async () => {
    serve({ "/vessels/live": LIVE, "/dark-events": { items: [] } });
    show(<LiveMap onOpenVessel={() => {}} />);
    expect(await screen.findByText("No events match these filters.")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Kind"), "position_jump");
    await waitFor(() => expect(calls.some((u) => u.includes("classification=position_jump"))).toBe(true));
    expect(calls.filter((u) => u.includes("/dark-events")).every((u) => u.includes("limit=200"))).toBe(true);
  });

  it("an empty fleet is explained, not shown as a blank map", async () => {
    serve({ "/vessels/live": { ...LIVE, data_time: null, vessels: [] }, "/dark-events": { items: [] } });
    show(<LiveMap onOpenVessel={() => {}} />);
    expect(await screen.findByText(/No positions have been ingested yet/)).toBeInTheDocument();
  });

  it("a feed that stopped is called stale", async () => {
    serve({ "/vessels/live": { ...LIVE, data_time: "2026-10-09T17:00:00Z" }, "/dark-events": { items: [] } });
    show(<LiveMap onOpenVessel={() => {}} />);
    expect(await screen.findByText(/The feed itself is stale/)).toBeInTheDocument();
  });

  it("a partial fleet is labelled partial", async () => {
    serve({ "/vessels/live": { ...LIVE, truncated: true }, "/dark-events": { items: [] } });
    show(<LiveMap onOpenVessel={() => {}} />);
    expect(await screen.findByText(/the map shows a partial fleet/)).toBeInTheDocument();
  });

  it("a failed first load offers a retry that works; a failed events load fails on its own", async () => {
    let up = false;
    serve({ "/vessels/live": () => (up ? LIVE : fail(503, "the database is unavailable; retry shortly")()), "/dark-events": fail(500, "boom") });
    show(<LiveMap onOpenVessel={() => {}} />);
    expect(await screen.findByText(/The fleet could not be loaded. the database is unavailable/)).toBeInTheDocument();
    expect(await screen.findByText(/Events could not be loaded. boom/)).toBeInTheDocument();
    up = true;
    await userEvent.click(screen.getAllByRole("button", { name: "Try again" })[0]);
    expect(await screen.findByText(/vessels tracked/)).toBeInTheDocument();
  });

  it("search finds by name, reports no match, and opens a typed MMSI directly", async () => {
    const open = vi.fn();
    serve({ "/vessels/live": LIVE, "/dark-events": { items: [] }, "q=nord": [{ mmsi: 211000951, name: "NORD TRADER", flag: "DE" }], "q=zzz": [] });
    show(<LiveMap onOpenVessel={open} />);
    const box = screen.getByLabelText("Find a vessel");
    await userEvent.type(box, "nord");
    await userEvent.click(await screen.findByRole("button", { name: /NORD TRADER/ }));
    expect(open).toHaveBeenCalledWith(211000951);
    await userEvent.clear(box);
    await userEvent.type(box, "zzz");
    expect(await screen.findByText("No vessel matches “zzz”.")).toBeInTheDocument();
    await userEvent.clear(box);
    await userEvent.type(box, "257000001{Enter}");
    expect(open).toHaveBeenLastCalledWith(257000001);
  });
});

describe("investigation", () => {
  it("loads the vessel, its track and its silences, and replays by keyboard", async () => {
    serve({ "/track": TRACK, "/dark-events": { items: [event()] }, "/vessels/477001198": VESSEL });
    show(<Investigation mmsi={477001198} onBack={() => {}} />);
    expect(screen.getByText("Loading vessel 477001198…")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "STAR QUEEN" })).toBeInTheDocument();
    expect(screen.getByText("Hong Kong (HK)")).toBeInTheDocument();
    expect(screen.getByText("not reported or invalid")).toBeInTheDocument(); // a missing IMO is said, not left blank
    expect(await screen.findByText(/3 positions over 50 min, in 2 unbroken stretches/)).toBeInTheDocument();
    const slider = screen.getByRole("slider");
    expect(slider).toHaveAttribute("aria-valuetext", "2026-10-09 10:00 UTC");
    // jsdom does not implement range-input keys; the end-to-end test drives the real slider by keyboard.
    fireEvent.change(slider, { target: { value: "1000" } });
    expect(slider).toHaveAttribute("aria-valuetext", "2026-10-09 10:50 UTC");
    expect(screen.getByRole("button", { name: "↺ Replay" })).toBeInTheDocument();
    // Jumping to a silence moves the replay to the moment the vessel vanished.
    await userEvent.click(await screen.findByRole("button", { name: /Went dark, impossible speed/ }));
    expect(slider).toHaveAttribute("aria-valuetext", "2026-10-09 10:01 UTC");
    expect(screen.getByText("12 other vessels heard nearby during the silence")).toBeInTheDocument();
  });

  it("an unknown vessel is a clear dead end with a way back", async () => {
    const back = vi.fn();
    serve({ "/vessels/257999999": fail(404, "no vessel with MMSI 257999999 has been seen") });
    show(<Investigation mmsi={257999999} onBack={back} />);
    expect(await screen.findByText("No vessel with MMSI 257999999 has been seen.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Back to the live map/ }));
    expect(back).toHaveBeenCalled();
  });

  it("a vessel with no positions and no silences says both", async () => {
    serve({ "/track": { ...TRACK, points: [] }, "/dark-events": { items: [] }, "/vessels/477001198": VESSEL });
    show(<Investigation mmsi={477001198} onBack={() => {}} />);
    expect(await screen.findByText("This vessel has identity data but no stored positions.")).toBeInTheDocument();
    expect(await screen.findByText(/None. This vessel has reported without unexplained gaps/)).toBeInTheDocument();
  });

  it("keeps the part of a track that loaded when a later page fails", async () => {
    let page = 0;
    serve({
      "/track": () => (page++ === 0 ? { ...TRACK, next_cursor: "abc" } : fail(503, "the database is unavailable; retry shortly")()),
      "/dark-events": { items: [] },
      "/vessels/477001198": VESSEL,
    });
    show(<Investigation mmsi={477001198} onBack={() => {}} />);
    expect(await screen.findByText(/Only the first 3 positions loaded; the rest failed/)).toBeInTheDocument();
    expect(screen.getByRole("slider")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load the rest" })).toBeInTheDocument();
  });

  it("a track that fails outright can be retried", async () => {
    let up = false;
    serve({ "/track": () => (up ? TRACK : fail(500, "boom")()), "/dark-events": { items: [] }, "/vessels/477001198": VESSEL });
    show(<Investigation mmsi={477001198} onBack={() => {}} />);
    expect(await screen.findByText(/The track could not be loaded. boom/)).toBeInTheDocument();
    up = true;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("slider")).toBeInTheDocument();
  });
});
