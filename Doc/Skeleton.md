# `/Skeleton` — nexus project mind diagram

**Diagram type:** Mermaid (renders in Obsidian, Notion, GitHub, most MD viewers)

```mermaid
flowchart TB
    GOAL(["🎯 nexus<br/>single-pass scraper service"])

    CONFIG[["📄 Config.json<br/>declarative surface"]]
    SERVER[["🐍 Scraper.py<br/>FastAPI runtime"]]
    CLIENT[["🌐 index.html<br/>client SPA"]]

    RUNTIME(["⚙️ Runtime deps<br/>curl-cffi · selectolax<br/>Pillow · argon2 · AES-GCM"])
    ROUTES(["🛣️ Routes<br/>/ · /api/health · /api/config<br/>/api/sites · /api/process"])
    PIPELINE(["🔗 Pipeline<br/>fetch → parse → extract<br/>→ clean → tokens → report/graph"])
    SITES(["📚 Site rules<br/>44 domain handlers"])
    APICLIENTS(["🔌 API clients<br/>GitHub · HuggingFace"])
    RTL(["🕌 RTL layer<br/>reshape · bidi · Noto font"])
    CAPS(["📉 Memory caps<br/>8MB fetch · 500 cache<br/>120 stars · 60 nodes"])
    GUARDS(["🛡️ Guards<br/>rate limit · SSRF · secrets"])

    ASSUME1{"assumption<br/>target sites<br/>serve SSR HTML"}
    ASSUME2{"assumption<br/>Arabic Wikipedia<br/>is primary corpus"}
    RISK1["⚠️ risk<br/>arabic-reshaper<br/>dep missing"]
    RISK2["⚠️ risk<br/>curl-cffi<br/>fingerprint names"]
    RISK3["⚠️ risk<br/>localStorage<br/>quota exceeded"]
    RISK4["⚠️ risk<br/>force-graph labels<br/>still overlap"]
    UNKNOWN1(["❓ unknown<br/>real crawl failure<br/>distribution"])
    UNKNOWN2(["❓ unknown<br/>AI-detect<br/>calibration"])

    GOAL --> CONFIG
    GOAL --> SERVER
    GOAL --> CLIENT

    CONFIG -->|loads| SERVER
    CONFIG -->|exposed via /api/config| CLIENT

    SERVER --> RUNTIME
    SERVER --> ROUTES
    ROUTES -->|POST| PIPELINE
    ROUTES -->|GET| SITES
    ROUTES -->|GET| APICLIENTS

    PIPELINE --> SITES
    PIPELINE --> APICLIENTS
    PIPELINE --> RTL
    PIPELINE --> GUARDS

    CLIENT -->|fetch| ROUTES
    CLIENT -->|renders| RTL
    CLIENT -->|state| CAPS

    ASSUME1 -.supports.-> PIPELINE
    ASSUME2 -.supports.-> RTL

    RTL -.depends_on.-> RISK1
    RUNTIME -.depends_on.-> RISK2
    CLIENT -.depends_on.-> RISK3
    CLIENT -.depends_on.-> RISK4

    PIPELINE -.contradicts.-> ASSUME1
    UNKNOWN1 -.risks.-> PIPELINE
    UNKNOWN2 -.risks.-> PIPELINE

    CAPS -.supports.-> SERVER
    GUARDS -.supports.-> ROUTES

    classDef goal fill:#000,stroke:#000,color:#fff,font-weight:bold
    classDef file fill:#f4f4f4,stroke:#000,stroke-width:2px
    classDef runtime fill:#fff,stroke:#000
    classDef risk fill:#fff4d6,stroke:#c8a032
    classDef unknown fill:#ececec,stroke:#737373,stroke-dasharray:4 3
    classDef assume fill:#fff,stroke:#000,stroke-dasharray:2 2

    class GOAL goal
    class CONFIG,SERVER,CLIENT file
    class RUNTIME,ROUTES,PIPELINE,SITES,APICLIENTS,RTL,CAPS,GUARDS runtime
    class RISK1,RISK2,RISK3,RISK4 risk
    class UNKNOWN1,UNKNOWN2 unknown
    class ASSUME1,ASSUME2 assume
```

## Legend

| Shape | Meaning |
|---|---|
| Black rounded rect | Root goal |
| Double-bracket rect | Concrete file on disk |
| Rounded rect (plain) | Runtime component |
| Diamond (dashed border) | Assumption that must hold |
| Triangle (amber) | Risk that would break a component |
| Cloud (gray, dashed) | Unknown requiring measurement |
| `-->` | Depends on / flows to |
| `-.->` | Weaker dependency (assumption, risk, or inference) |

## Edge labels

- **loads** — Config.json is read by Scraper.py at startup
- **exposed via** — Config.json values surface to the client through `/api/config`
- **fetch** — Client posts to the server
- **renders** — Client renders RTL text, graphs, reports
- **depends_on** — Failure here breaks the target
- **contradicts** — If reality differs, the assumption fails
- **risks** — Unknown that could invalidate
- **supports** — Reinforces

## Critical path

`GOAL → CONFIG → SERVER → RUNTIME → ROUTES → PIPELINE → RTL → CLIENT`

Every node on this path is a hard dependency. If `Config.json` is malformed, `Scraper.py` won't boot. If `Scraper.py` can't import `curl_cffi`, the pipeline is dead. If RTL shaping is missing, the Arabic report is broken.

## Highest-risk node

**`RISK1 — arabic-reshaper missing`**. It sits directly on the critical path (`RTL -.depends_on.-> RISK1`), it's an optional dependency that silently no-ops, and it produces output that *looks* fine to a Latin reader while being illegible to an Arabic reader. The failure mode is invisible to anyone testing with English content, which is exactly the failure the user reported. It's the only risk on the diagram that both (a) breaks the primary use case shown in the screenshot and (b) is undetectable without deliberately testing Arabic input.