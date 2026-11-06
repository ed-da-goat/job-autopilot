# Seed Company List — Job-Search Harvester

Compiled via web search (WebSearch tool) plus read-only spot-checks against public ATS
JSON APIs (curl, 1 req/sec, no auth). Candidate: Example State University Dec-2026 grad, B.S.
Business Administration (finance concentration), founder-engineer (solo-built a deployed
multi-tenant AI email-drafting agent: FastAPI/async SQLAlchemy/Postgres RLS/human-approval
triggers/append-only audit/AES-256-GCM/Fly.io), personally ran outbound sales. Targets:
AI/agent engineering, backend/full-stack SWE, founding/early-startup engineer,
forward-deployed & solutions engineering, sales/GTM engineering, technical consulting,
fintech engineering. Open to SF/NYC/anywhere, start Dec 2026/Jan 2027, up to ~3 yrs exp in range.

**Verified column key:**
- `yes` — curl'd the public API endpoint directly (boards-api.greenhouse.io, api.ashbyhq.com,
  api.lever.co), got HTTP 200 with a real JSON job list.
- `no` — curl'd a guessed slug and it 404'd (or search text conflicted); slug/platform shown
  is a best guess and needs manual confirmation before use.
- `untested` — platform/slug sourced from search-result text (careers-page links, job-posting
  URLs) or from general knowledge of the company's ATS, not independently curled.

33 live API calls were made across 27 distinct company guesses (well within the 25-slug
spot-check allowance once corrected re-guesses are collapsed to their final slug); 19 came
back `yes`. Where a first guess 404'd and a corrected guess then returned 200 (e.g. Anduril,
Cursor), only the working slug appears below.

**2026-07-17 slug-repair sweep:** 46 companies flagged 404 by an automated harvester run
(see `data/bad_slugs.md`) were re-searched and re-verified. 31 were repaired and
curl-confirmed (`yes`) — most were platform migrations (Greenhouse/Lever to Ashby, or
vice versa) or slug renames the harvester's stale guess didn't catch. 3 (Codeium/Windsurf,
Naptha AI, Statsig) have confirmed-live job postings visible on their Ashby careers pages
but the posting-api endpoint 404's for every slug variant tried — left as `no` pending
manual confirmation. 12 have no public board on Greenhouse/Lever/Ashby at all and were
moved to the watchlist section below (Groq, Fly.io, Cal.com, Chroma, Fondo, Galileo,
Humanloop, Increase, Openlayer, Truewind, Val Town, Voiceflow).

**2026-08-03 404-sweep resolutions:** boards confirmed gone were retired — slug cleared
here to match the DB and `data/bad_slugs.md` (Codeium/Windsurf → covered by Cognition's
board; Statsig → acquired by OpenAI; Naptha AI and Contextual AI → boards removed). dbt
Labs merged with Fivetran: its `dbtlabsinc` row was replaced by Fivetran (greenhouse,
slug `fivetran`, curl-confirmed live).

## Company table

| Company | Category | Platform | Slug | Verified | Why-fit |
|---|---|---|---|---|---|
| Anthropic | ai-infra | greenhouse | anthropic | yes | Frontier lab; Applied AI / Forward-Deployed Engineering team is the exact FDE + agent-shipping profile the candidate already has |
| OpenAI | ai-infra | ashby | openai | yes | Frontier lab, large new-grad + forward-deployed hiring surface |
| Together AI | ai-infra | greenhouse | togetherai | yes | AI inference/infra platform, backend-heavy roles |
| Scale AI | ai-infra | greenhouse | scaleai | yes | Enterprise GenAI infra roles explicitly list LLM + vector DB experience |
| Glean | ai-infra | greenhouse | gleanwork | yes | Enterprise AI search/agents, ML + agent engineering roles |
| Cohere | ai-infra | ashby | cohere | yes | LLM platform; "Applied AI Engineer, Agentic Workflows" matches candidate's shipped agent |
| Modal | ai-infra | ashby | modal | yes | Serverless GPU/AI infra, strong backend-systems bar |
| Sierra | ai-infra | ashby | sierra | yes | Conversational AI agents for enterprise, applied-AI engineering |
| Cursor (Anysphere) | ai-infra | ashby | cursor | yes | AI coding agent, elite generalist-engineer bar, high comp |
| LangChain | ai-infra | ashby | langchain | yes | Agent orchestration framework, core dev-tooling for agent builders |
| Harvey | ai-infra | ashby | harvey | untested | Legal AI agents, applied-AI engineering track |
| Decagon | ai-infra | ashby | decagon | yes | Customer-service AI agents, Applied AI Engineer roles |
| Perplexity AI | ai-infra | ashby | perplexity | yes | AI search; switched from Greenhouse to Ashby, confirmed via API |
| Baseten | ai-infra | ashby | baseten | untested | Model deployment infra, backend/infra engineering |
| Fireworks AI | ai-infra | ashby | fireworksai | untested | LLM inference infra |
| Pinecone | ai-infra | ashby | pinecone | yes | Vector DB, core RAG infra; switched from Greenhouse to Ashby, confirmed via API |
| Weaviate | ai-infra | ashby | weaviate | untested | Vector DB / retrieval infra |
| Braintrust | ai-infra | ashby | braintrust | untested | LLM eval/observability, small elite eng team |
| Cognition AI | ai-infra | ashby | cognition | yes | Devin autonomous coding agent; slug is `cognition` not `cognitionai`, confirmed via API |
| Runpod | ai-infra | ashby | runpod | untested | GPU cloud for AI workloads |
| CoreWeave | ai-infra | greenhouse | coreweave | untested | GPU cloud infra at scale |
| LlamaIndex | ai-infra | ashby | llamaindex | untested | RAG/agent framework |
| Arize AI | ai-infra | greenhouse | arizeai | yes | ML/LLM observability; slug is `arizeai` not `arize`, confirmed via API |
| Langfuse | ai-infra | ashby | langfuse | untested | Open-source LLM observability |
| Mistral AI | ai-infra | lever | mistral | yes | Frontier open-weight lab, growing US presence; on Lever not Ashby, confirmed via API |
| ElevenLabs | ai-infra | ashby | elevenlabs | untested | Voice AI, applied engineering |
| Replit | ai-infra | ashby | replit | untested | AI coding platform, YC-adjacent scale-up |
| Vapi | ai-infra | ashby | vapi | untested | Voice AI agent infra |
| Hebbia | ai-infra | ashby | hebbia-ai | yes | Enterprise AI search for finance/legal — direct finance-degree overlap; slug is `hebbia-ai`, confirmed via API |
| Writer | ai-infra | ashby | writer | yes | Enterprise generative AI platform; switched from Greenhouse to Ashby, confirmed via API |
| Imbue | ai-infra | greenhouse | imbue | yes | AI agent reasoning research lab; on Greenhouse not Ashby, confirmed via API |
| Contextual AI | ai-infra | greenhouse |  | yes | Enterprise RAG/agents; board retired 2026-08-03 (removed, careers page lists zero open roles — see bad_slugs.md) |
| Codeium (Windsurf) | ai-infra | ashby |  | no | AI coding agent; board retired 2026-08-03 — acquired by Cognition, whose `ashby/cognition` board (above) already covers it |
| Qdrant | ai-infra | ashby | qdrant.tech | yes | Vector DB infra; switched from Greenhouse to Ashby, slug is `qdrant.tech`, confirmed via API |
| Vellum AI | ai-infra | ashby | vellum | untested | LLM app dev platform |
| Letta (formerly MemGPT) | ai-infra | ashby | letta | untested | Stateful AI agent infra |
| OpenRouter | ai-infra | ashby | openrouter | untested | Model-routing infra — the exact vendor the candidate's own product is built on |
| Naptha AI | ai-infra | ashby |  | no | Multi-agent orchestration infra; board retired 2026-08-03 — careers page still embeds it but the public posting API 404s (disabled or empty) |
| Glacis AI | ai-infra | ashby | glacis-ai | untested | Applied agentic AI (search-confirmed careers link) |
| xAI | ai-infra | greenhouse | xai | untested | Frontier lab, backend/infra roles |
| Character.AI | ai-infra | ashby | character | yes | Applied conversational AI at scale; switched from Greenhouse to Ashby, slug is `character`, confirmed via API |
| Vercel | devtools | greenhouse | vercel | yes | Frontend/infra platform; async, API-first backend engineering |
| Temporal Technologies | devtools | greenhouse | temporaltechnologies | yes | Durable-execution infra, matches async-systems background |
| WorkOS | devtools | ashby | workos | yes | Enterprise-ready auth/identity APIs — directly parallel to candidate's own auth/RLS/encryption work |
| Supabase | devtools | ashby | supabase | yes | Postgres-as-a-platform, direct stack overlap (candidate uses Postgres+RLS); switched to Ashby, confirmed via API |
| PostHog | devtools | ashby | posthog | untested | Open-source product analytics, full-stack eng |
| Linear | devtools | ashby | linear | untested | High-craft project-management tool, small elite team |
| GitLab | devtools | greenhouse | gitlab | untested | DevOps platform, large distributed eng org |
| Postman | devtools | greenhouse | postman | untested | API tooling |
| Neon | devtools | ashby | neon | untested | Serverless Postgres — direct DB-stack overlap |
| PlanetScale | devtools | greenhouse | planetscale | untested | Serverless MySQL platform |
| Warp | devtools | ashby | warp | untested | AI-native terminal, small high-bar team |
| Render | devtools | ashby | render | yes | App hosting/PaaS — same use-case as candidate's own Fly.io deploy; switched to Ashby, confirmed via API |
| Railway | devtools | ashby | railway | untested | Deployment platform, small team |
| Sourcegraph | devtools | greenhouse | sourcegraph91 | yes | Code search/AI dev tooling; on Greenhouse not Lever, slug is `sourcegraph91`, confirmed via API |
| Docker | devtools | ashby | docker | yes | Core dev infrastructure; switched to Ashby, confirmed via API |
| Grafana Labs | devtools | greenhouse | grafanalabs | untested | Observability infra |
| Datadog | devtools | greenhouse | datadog | untested | Observability/monitoring at scale |
| Clerk | devtools | ashby | clerk | untested | Auth-as-a-service, overlaps candidate's own auth/session work |
| Neo4j | devtools | greenhouse | neo4j | untested | Graph DB infra |
| Airbyte | devtools | ashby | airbyte | untested | Open-source data integration |
| Fivetran | devtools | greenhouse | fivetran | yes | Data movement/ELT platform; absorbed dbt Labs in the 2026 merger (the `dbtlabsinc` board is retired), slug `fivetran` confirmed via API 2026-08-03 |
| Vanta | devtools | ashby | vanta | untested | Security/compliance automation (search-confirmed careers link) |
| Common Room | devtools | ashby | commonroom | untested | GTM/community intelligence platform |
| Chronosphere | devtools | ashby | chronospherejobs | yes | Cloud-native observability; switched to Ashby, slug is `chronospherejobs`, confirmed via API |
| Statsig | devtools | ashby |  | no | Feature flagging/experimentation infra; board retired 2026-08-03 — acquired by OpenAI, board gone |
| Ramp | fintech | ashby | ramp | yes | Applied AI Engineer roles on spend infra — automation for finance ops, strong finance-degree + AI overlap |
| Mercury | fintech | greenhouse | mercury | untested | Startup banking platform (search-confirmed careers link) |
| Plaid | fintech | ashby | plaid | yes | Bank-data infra APIs |
| Modern Treasury | fintech | ashby | moderntreasury | untested | Payments infrastructure APIs (search-confirmed careers link) |
| Anrok | fintech | ashby | anrok | untested | Sales-tax compliance infra for software companies (search-confirmed careers link) |
| Highbeam | fintech | ashby | highbeam | untested | AI-driven business banking/cash-mgmt (search-confirmed careers link) |
| Column | fintech | ashby | column | untested | Bank-chartered fintech infra (search-confirmed careers link) |
| Affirm | fintech | greenhouse | affirm | untested | BNPL/consumer credit at scale (search-confirmed careers link) |
| Brex | fintech | greenhouse | brex | untested | Corporate card/spend management |
| Stripe | fintech | greenhouse | stripe | untested | Payments infra, huge new-grad + backend hiring surface |
| Coinbase | fintech | greenhouse | coinbase | untested | Crypto exchange/fintech infra |
| Robinhood | fintech | greenhouse | robinhood | untested | Consumer trading/fintech, finance-degree overlap |
| Chime | fintech | greenhouse | chime | untested | Consumer neobank |
| Unit | fintech | ashby | unit | yes | Banking-as-a-service infra; switched to Ashby, confirmed via API |
| Wealthfront | fintech | lever | wealthfront | yes | Automated investing, finance-degree overlap; on Lever not Greenhouse, confirmed via API |
| Addepar | fintech | greenhouse | addepar1 | yes | Wealth-management data platform, direct finance-degree fit; slug is `addepar1` not `addepar`, confirmed via API |
| Carta | fintech | greenhouse | carta | untested | Cap-table/equity management, finance-degree fit |
| Alloy | fintech | lever | alloy | untested | Identity/fraud decisioning infra for banks |
| Melio | fintech | greenhouse | melio | untested | B2B payments infra |
| Persona | fintech | ashby | persona | untested | Identity verification infra |
| Sardine | fintech | ashby | sardine | untested | Fraud/risk infra for fintechs |
| Parafin | fintech | ashby | parafin | untested | Embedded fintech lending infra |
| Anchorage Digital | fintech | lever | anchorage | yes | Crypto custody, regulated fintech infra; on Lever not Greenhouse, confirmed via API |
| Fireblocks | fintech | greenhouse | fireblocks | untested | Digital-asset infra |
| Basis | fintech | ashby | basis-ai | untested | AI accounting agents — direct finance-degree + agent-shipping overlap (search-confirmed careers link) |
| Synctera | fintech | ashby | synctera | yes | Banking-as-a-service platform; switched to Ashby, confirmed via API |
| Method Financial | fintech | ashby | method | yes | Consumer liability data API; slug is `method` not `methodfi`, confirmed via API |
| Doji | yc-early | ashby | doji | untested | YC-backed, AI shopping, founding-engineer-style hiring (search-confirmed) |
| Seamflow | yc-early | ashby | seamflow | untested | Stealth/early team, founding engineer role (search-confirmed) |
| Legion Health | yc-early | ashby | legionhealth | untested | YC S-stage, Node/TS + Postgres + LLM agents in production (search-confirmed) |
| Applied Labs | yc-early | ashby | appliedlabs | untested | Early-stage, founding engineer (search-confirmed) |
| Ropes | yc-early | ashby | ropes | untested | Seed-stage, founding engineer (search-confirmed) |
| Anagram | yc-early | ashby | anagram | untested | Early-stage, founding engineer (search-confirmed) |
| Usul | yc-early | ashby | usul | untested | YC S24, founding fullstack engineer (search-confirmed) |
| incident.io | yc-early | ashby | incident | yes | Fast-growing YC-adjacent infra co, actively hiring per search; slug is `incident` not `incidentio`, confirmed via API |
| Pylon | yc-early | ashby | pylon | untested | B2B support infra, active YC-stage hiring |
| Mintlify | yc-early | ashby | mintlify | untested | YC dev-tool (docs platform), small team |
| Resend | yc-early | ashby | resend | untested | YC dev-tool (email API), small elite team |
| Endex | yc-early | ashby | endex | untested | AI finance research agents — direct finance-degree + agent fit |
| Momentic | yc-early | ashby | momentic | untested | AI QA/testing agents, YC-stage |
| Campfire | yc-early | ashby | campfire | untested | Modern ERP/accounting platform, early-stage |
| Clay | yc-early | unknown | unknown | no | GTM data/orchestration platform used by many YC cos; no ATS link surfaced in search — confirm manually |
| Palantir | fde-solutions | lever | palantir | yes | Pioneered the Forward Deployed Engineer role; embeds engineers directly with customers |
| Endeavor | fde-solutions | ashby | endeavorai | yes | Early-career Forward Deployed Engineer role verified live on the public Ashby API on 2026-08-02; direct customer deployment plus applied-AI fit |
| Anduril Industries | fde-solutions | greenhouse | andurilindustries | yes | Defense tech, high-velocity software + forward-deployed engineering |
| Applied Intuition | fde-solutions | ashby | applied | yes | Autonomy/defense software; on Ashby not Greenhouse, slug is `applied`, confirmed via API |
| Shield AI | fde-solutions | lever | shieldai | yes | Autonomous defense systems, field/solutions engineering; on Lever not Greenhouse, confirmed via API |
| Saronic | fde-solutions | lever | saronic | yes | Autonomous defense vessels, early-stage systems engineering; on Lever not Greenhouse, confirmed via API |
| Vannevar Labs | fde-solutions | greenhouse | vannevarlabs | yes | Defense/national-security AI, forward-deployed style roles; on Greenhouse not Ashby, confirmed via API |
| Epirus | fde-solutions | greenhouse | epirus | untested | Defense tech hardware/software integration |
| Rebellion Defense | fde-solutions | greenhouse | rebelliondefense | untested | Defense AI software, government-facing solutions engineering |
| Databricks | fde-solutions | greenhouse | databricks | yes | Field Engineering / Forward Deployed teams embed with enterprise data/AI customers |
| C3.ai | fde-solutions | greenhouse | c3iot | yes | Enterprise AI applications, customer-facing solutions engineering; slug is `c3iot` not `c3ai`, confirmed via API |
| Second Front Systems | fde-solutions | ashby | Second-Front-Systems | yes | Dual-use gov software, DevSecOps/solutions engineering; on Ashby not Greenhouse, confirmed via API |
| Skydio | fde-solutions | ashby | skydio | yes | Autonomous drones, field/deployment engineering; switched to Ashby, confirmed via API |
| Rogo | fintech | ashby | rogo | yes | AI analyst for investment banking/PE — the purest finance×AI hybrid; 73 live roles, curl-confirmed 2026-08-05 |
| Kalshi | fintech | ashby | kalshi | yes | Regulated prediction-market exchange — trading/markets domain + heavy engineering; curl-confirmed 2026-08-05 |
| Taktile | fintech | ashby | taktile | yes | AI decisioning platform for credit/risk teams — finance-domain ML infra; curl-confirmed 2026-08-05 |
| Imprint | fintech | ashby | imprint | yes | Co-branded credit cards, modern card issuance stack; curl-confirmed 2026-08-05 |
| Numeric | fintech | ashby | numeric | yes | AI-powered accounting close automation — finance-ops×AI; curl-confirmed 2026-08-05 |
| SoFi | fintech | greenhouse | sofi | yes | Consumer fintech at scale — lending/banking/invest; curl-confirmed 2026-08-05 |
| Betterment | fintech | greenhouse | betterment | yes | Robo-advisor/wealth management — investing domain; curl-confirmed 2026-08-05 |
| Marqeta | fintech | greenhouse | marqeta | yes | Card-issuing infrastructure APIs; curl-confirmed 2026-08-05 |
| Lithic | fintech | greenhouse | lithic | yes | Card-issuing API for developers; small board, curl-confirmed 2026-08-05 |
| Slope | fintech | ashby | slope | yes | B2B payments/AI credit underwriting — board live but 0 open roles on 2026-08-05; keep for future sweeps |

## Companies known to use none of the three platforms (manual watchlist)

These are strong-fit companies where search evidence points to Workday, a custom
in-house ATS, or no clear ATS link at all — track manually, don't feed to the
Greenhouse/Lever/Ashby harvester:

- **Meta** — proprietary careers site (metacareers.com), not on Greenhouse/Lever/Ashby.
- **Google / Alphabet** — proprietary careers site (careers.google.com).
- **Microsoft** — proprietary careers site (careers.microsoft.com).
- **Amazon** — proprietary careers site (amazon.jobs).
- **Snowflake** — widely reported to run on Workday, not the big three.
- **Moov (Moov Financial)** — search surfaced a custom careers.moov.io / Built-In listing,
  no Greenhouse/Lever/Ashby link found.
- **Rippling** — guessed Ashby slug 404'd and no confirming search hit for
  Greenhouse/Lever either; likely a custom or unlisted-token ATS — verify manually.
- **Deel** — same situation as Rippling: guessed Greenhouse slugs (deel, deelcareers)
  both 404'd; no Lever/Ashby evidence surfaced — verify manually.
- **HashiCorp** — search shows an old-style `boards.greenhouse.io/hashicorp` careers
  link, but the current `boards-api.greenhouse.io` token 404'd (likely mid-migration
  post-IBM-acquisition ATS changes) — verify manually before harvesting.
- **Retool** — no confirmed Greenhouse or Lever slug; both direct guesses 404'd —
  verify manually.
- **Groq** — uses Gem, not Greenhouse; live board at jobs.gem.com/groq.
- **Fly.io** — no open positions and no ATS link found as of this sweep.
- **Cal.com** — custom /jobs page on cal.com, not on Greenhouse/Lever/Ashby.
- **Chroma** — custom careers page on trychroma.com, not on the big three.
- **Fondo** — fondohq.com is now a parked/for-sale domain; no ATS link found on
  fondo.com or in current listings (LinkedIn-only) — verify manually.
- **Galileo (rungalileo.io)** — uses Rippling ATS; board at ats.rippling.com/galileo/jobs.
- **Humanloop** — acquired/absorbed by Anthropic (Aug 2025); platform sunset
  Sept 2025 and no longer hiring independently.
- **Increase** — no careers page or ATS link found; small (~15-person) team.
- **Openlayer** — no open positions currently and no ATS link found.
- **Truewind** — custom career pages on truewind.ai, not on the big three.
- **Val Town** — postings via a Notion page + direct email, no ATS.
- **Voiceflow** — uses Breezy HR; board at voiceflow.breezy.hr.
