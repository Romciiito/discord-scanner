# Market Analysis: discord-scanner (Stage 2)

**Status:** STUB — market analysis was intentionally skipped for this project. `discord-scanner` is an internal single-user Stage-2 pipeline component with no commercial parallel. This document exists so that downstream agents (architect, workplan-builder) have the expected input file and do not halt on a missing reference.

## Context

Internal tool. No market, no buyers, no pricing. Part of a four-stage personal-research pipeline (Stage 1: civit-hf-scanner → Stage 2: discord-scanner → Stage 3: discord-curator → Stage 4: content workflows).

## Adjacent tools (not competitors)

- **`discord.py` / `pycord`**: full bot frameworks. Require bot token + server admin invitation, which this use case does not have. Not applicable.
- **`discord.py-self`**: user-token self-bot fork. Unmaintained, still against ToS. We implement our own tighter subset directly via httpx + websockets for surgical control over fingerprint and discipline.
- **`dsc-smartscraper`** (user's prior tool): the direct predecessor this repo is rebuilding. Analysed in depth; inheritance + rewrite plan captured in `brainstorm.md §9`.
- **Generic scrapers** (Cloudflare-bypass libraries, etc.): irrelevant — Discord is not the same blocking surface.

## Competitive landscape

N/A — this is an internal pipeline component. No commercial substitute exists for "burner-token Discord scraper with strict anti-detection discipline and JSONL-dump output contract for a downstream curator." The closest public attempts are all `discord.py-self` forks which are unmaintained.

## Architectural implications

None beyond what's already fixed in `seed-spec.md`:
- Single-user local CLI, no multi-tenant concerns
- No commercial-differentiation pressure on architecture
- Optimize for correctness, anti-detection discipline, observability, idempotence

## Decision

Skip deeper market analysis. Downstream agents: use this stub as the satisfied input and proceed.
