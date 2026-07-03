const MILESTONES: { id: string; theme: string; status: string }[] = [
  { id: "M0a", theme: "Shortest vertical slice (config → IR → Aureus 3-step chain)", status: "in progress" },
  { id: "M0b", theme: "Aureus combat/economy + Schema Registry round-trip + version/lineage", status: "planned" },
  { id: "M1", theme: "Graph/ASP/SMT checkers + DSL compile + economy sim", status: "planned" },
  { id: "M2", theme: "Bounded LLM agents + Playtest Agent + regression (cassette)", status: "planned" },
  { id: "M3", theme: "GameForge-Bench + full metrics + Eval panel", status: "planned" },
  { id: "M4", theme: "Production hardening: observability, lineage, RBAC, full pages", status: "planned" },
];

export default function App() {
  return (
    <main style={{ fontFamily: "system-ui, sans-serif", maxWidth: 780, margin: "0 auto", padding: 24 }}>
      <h1 style={{ marginBottom: 4 }}>GameForge Console</h1>
      <p style={{ color: "#555", marginTop: 0 }}>
        Correctness compiler + agent workbench for game content — scaffold (M0a).
      </p>
      <h2 style={{ fontSize: 16 }}>Milestone map</h2>
      <ul style={{ lineHeight: 1.6 }}>
        {MILESTONES.map((m) => (
          <li key={m.id}>
            <strong>{m.id}</strong> — {m.theme} <em style={{ color: "#888" }}>({m.status})</em>
          </li>
        ))}
      </ul>
      <p style={{ color: "#888", fontSize: 13 }}>
        Pages (Spec/KG, Generation, Review, Playtest, Patch, Eval, Observability, Approvals) land in M4.
      </p>
    </main>
  );
}
