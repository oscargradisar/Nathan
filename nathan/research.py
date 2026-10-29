"""Research mode: answer niche questions from real sources, not from model memory.

Models alone confidently invent niche facts. So Nathan first gathers evidence from free,
official, keyless sources, then several models answer *from that evidence* with
citations, and a verifier cross-checks them into one cited answer with a confidence level.

  1. scout     a quick model turns the question into a few search queries
  2. search    Wikipedia, DuckDuckGo instant answers, Stack Overflow, OpenAlex (papers),
               arXiv and Hacker News, all at once; pages HN links to are read too
  3. analysts  several models on different providers answer from the evidence only
  4. verifier  merges, checks claims against the sources, notes disagreements

Events: phase, working, delivered, task_failed (with role scout/source/analyst/verifier),
sources, final.
"""

from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote

import httpx2 as httpx
from pydantic import BaseModel, Field

from nathan.errors import NoModelAvailable
from nathan.jsonutil import race_json
from nathan.tools.fetch import html_to_text

Emit = Callable[[dict[str, Any]], Awaitable[None] | None]
UA = {"user-agent": "Nathan/1.0 (free multi-model research app; github.com/oscargradisar/Nathan)"}
SNIPPET = 1800        # characters of text kept per source item
MAX_EVIDENCE = 16     # items handed to the analysts
ANALYSTS = 4

SCOUT_SYSTEM = """You plan web research. Turn the question into 2-4 short search queries
(keywords, not sentences) that would find the answer in Wikipedia, papers or forums.
Reply with ONLY JSON: {"queries": ["...", "..."]}"""

ANALYST_SYSTEM = """You are a research analyst. Answer the question using the numbered
EVIDENCE below. Cite every factual claim like [3]. If the evidence doesn't cover something,
say so plainly ("not in the sources") and only then add what you know, marked as unsourced.
Be specific: names, numbers, dates. No filler."""

VERIFIER_SYSTEM = """You are the verifier. Several analysts answered the same question from
the same numbered EVIDENCE. Write the final answer:
- Keep only claims supported by the evidence or agreed by several analysts; cite them like [2].
- Where analysts disagree, say so and say which the evidence supports.
- End with a line "Confidence: high|medium|low — <why>".
Answer the person directly; don't mention analysts."""


class Queries(BaseModel):
    queries: list[str] = Field(default_factory=list)


@dataclass
class Evidence:
    n: int
    source: str
    title: str
    url: str
    text: str


async def _emit(emit: Emit | None, event: dict[str, Any]) -> None:
    if emit is not None:
        out = emit(event)
        if asyncio.iscoroutine(out):
            await out


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html_to_text(text or "")).strip()[:SNIPPET]


# ---- sources: each returns [(title, url, text)] ----------------------------------------

async def wikipedia(http, q: str) -> list[tuple[str, str, str]]:
    r = await http.get("https://en.wikipedia.org/w/api.php", params={
        "action": "query", "generator": "search", "gsrsearch": q, "gsrlimit": 3, "prop": "extracts|info",
        "inprop": "url", "explaintext": 1, "exchars": SNIPPET, "exlimit": 3, "format": "json"})
    pages = (r.json().get("query") or {}).get("pages") or {}
    return [(p["title"], p.get("fullurl", ""), p.get("extract", "")) for p in
            sorted(pages.values(), key=lambda p: p.get("index", 0)) if p.get("extract")]


async def duckduckgo(http, q: str) -> list[tuple[str, str, str]]:
    j = (await http.get("https://api.duckduckgo.com/", params={
        "q": q, "format": "json", "no_html": 1, "skip_disambig": 1})).json()
    out = []
    if j.get("AbstractText"):
        out.append((j.get("Heading") or q, j.get("AbstractURL", ""), j["AbstractText"]))
    if j.get("Answer"):
        out.append((f"Instant answer: {q}", j.get("AbstractURL", ""), str(j["Answer"])))
    return out


async def stackoverflow(http, q: str) -> list[tuple[str, str, str]]:
    j = (await http.get("https://api.stackexchange.com/2.3/search/advanced", params={
        "order": "desc", "sort": "relevance", "q": q, "site": "stackoverflow", "filter": "withbody",
        "pagesize": 2, "accepted": "True"})).json()
    return [(_clean(i["title"]), i["link"], i.get("body", "")) for i in j.get("items", [])]


async def openalex(http, q: str) -> list[tuple[str, str, str]]:
    j = (await http.get("https://api.openalex.org/works", params={"search": q, "per-page": 3})).json()
    out = []
    for w in j.get("results", []):
        inv = w.get("abstract_inverted_index") or {}
        words = sorted((pos, word) for word, poss in inv.items() for pos in poss)
        abstract = " ".join(word for _, word in words)
        if abstract:
            year = w.get("publication_year") or ""
            out.append((f"{w.get('title') or 'paper'} ({year})", w.get("doi") or w.get("id", ""), abstract))
    return out


async def arxiv(http, q: str) -> list[tuple[str, str, str]]:
    r = await http.get(f"https://export.arxiv.org/api/query?search_query=all:{quote(q)}&max_results=2")
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(r.text).findall("a:entry", ns):
        title = (e.findtext("a:title", "", ns) or "").strip()
        out.append((title, (e.findtext("a:id", "", ns) or "").strip(), e.findtext("a:summary", "", ns) or ""))
    return out


async def hackernews(http, q: str) -> list[tuple[str, str, str]]:
    j = (await http.get("https://hn.algolia.com/api/v1/search", params={
        "query": q, "tags": "story", "hitsPerPage": 3})).json()
    out = []
    for h in j.get("hits", []):
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        text = h.get("story_text") or ""
        if h.get("url"):  # read the linked page itself: that's where the facts are
            try:
                page = await http.get(h["url"], follow_redirects=True)
                if "html" in page.headers.get("content-type", ""):
                    text = page.text[:200_000]
            except httpx.HTTPError:
                pass
        if text:
            out.append((h.get("title") or url, url, text))
    return out


SOURCES = {"Wikipedia": wikipedia, "DuckDuckGo": duckduckgo, "Stack Overflow": stackoverflow,
           "OpenAlex": openalex, "arXiv": arxiv, "Hacker News": hackernews}


async def gather_evidence(queries: list[str], emit: Emit | None = None,
                          http: httpx.AsyncClient | None = None) -> list[Evidence]:
    own = http is None
    http = http or httpx.AsyncClient(timeout=12, headers=UA, follow_redirects=True)
    found: dict[str, list[tuple[str, str, str]]] = {}

    async def one(name: str) -> None:
        await _emit(emit, {"type": "working", "id": "src:" + name, "worker": name, "role": "source"})
        items: list[tuple[str, str, str]] = []
        for q in queries:
            try:
                items += await SOURCES[name](http, q)
            except (httpx.HTTPError, ValueError, ET.ParseError, KeyError):
                continue  # one source failing must not sink the research
        found[name] = items
        await _emit(emit, {"type": "delivered" if items else "task_failed", "id": "src:" + name,
                           "worker": name, "role": "source", "seconds": None,
                           "text": "\n".join(f"• {t}" for t, _, _ in items[:6]) or "nothing found",
                           "reason": "nothing found"})

    try:
        await asyncio.gather(*(one(n) for n in SOURCES))
    finally:
        if own:
            await http.aclose()
    # Round-robin across sources so one chatty source can't crowd out the rest
    evidence: list[Evidence] = []
    seen: set[str] = set()
    pools = [list(v) for v in found.values()]
    names = list(found)
    while pools and len(evidence) < MAX_EVIDENCE and any(pools):
        for name, pool in zip(names, pools, strict=True):
            if pool and len(evidence) < MAX_EVIDENCE:
                title, url, text = pool.pop(0)
                key = url or title
                if key in seen or not _clean(text):
                    continue
                seen.add(key)
                evidence.append(Evidence(len(evidence) + 1, name, _clean(title)[:200], url, _clean(text)))
    return evidence


def evidence_block(evidence: list[Evidence]) -> str:
    return "\n\n".join(f"[{e.n}] {e.title} — {e.source} ({e.url})\n{e.text}" for e in evidence)


PREMIUM_BRIEF = """Research this question thoroughly using live web search. Give a precise,
specific answer (names, numbers, dates), cite the pages you used as markdown links, and say
what you couldn't confirm.

QUESTION: {q}"""


async def _premium(m, question: str, emit: Emit | None, ask) -> tuple[str, str] | None:
    unit = f"premium:{m.id}"
    await _emit(emit, {"type": "working", "id": unit, "worker": m.name, "role": "premium researcher"})
    try:
        text = await ask(m, PREMIUM_BRIEF.format(q=question), web=True)
    except RuntimeError as e:
        await _emit(emit, {"type": "task_failed", "id": unit, "worker": m.name,
                           "role": "premium researcher", "reason": str(e)})
        return None
    await _emit(emit, {"type": "delivered", "id": unit, "worker": m.name, "role": "premium researcher",
                       "text": text, "seconds": None})
    return m.name, text


async def run_research(engine, question: str, emit: Emit | None = None,
                       http: httpx.AsyncClient | None = None, team=None, ask=None) -> dict:
    from nathan import managers
    from nathan.workflow import hedged

    ask = ask or managers.ask
    team = team if team is not None else await managers.connected()
    # Your premium plans research the live web themselves, alongside the free pipeline
    premium_task = asyncio.gather(*(_premium(m, question, emit, ask) for m in team))

    # 1. scout
    await _emit(emit, {"type": "phase", "phase": "scouting"})
    scouts = engine.fastest(["fast", "cheap"])[:4]
    await _emit(emit, {"type": "working", "id": "scout", "worker": scouts[0] if scouts else "", "role": "scout"})
    try:
        plan, scout = await asyncio.wait_for(race_json(engine.client, scouts, [
            {"role": "system", "content": SCOUT_SYSTEM}, {"role": "user", "content": question[:2000]}],
            Queries, max_tokens=300, temperature=0), 12)
        queries = [q.strip() for q in plan.queries if q.strip()][:4] or [question[:200]]
    except (NoModelAvailable, TimeoutError):
        queries, scout = [question[:200]], "keyword fallback"
    await _emit(emit, {"type": "delivered", "id": "scout", "worker": scout, "role": "scout",
                       "text": "\n".join(queries), "seconds": None})

    # 2. search
    await _emit(emit, {"type": "phase", "phase": "searching"})
    evidence = await gather_evidence(queries, emit, http)
    await _emit(emit, {"type": "sources", "items": [asdict(e) | {"text": e.text[:300]} for e in evidence]})
    block = evidence_block(evidence) or "(no sources found: answer from knowledge and say so)"

    # 3. analysts, on different providers, each with backups
    await _emit(emit, {"type": "phase", "phase": "analysing"})
    picks = engine.roster.pick(["reasoning", "writing"], ANALYSTS)
    spare = [r for r in engine.chain(["reasoning", "writing"]) if r not in picks]
    msgs = [{"role": "system", "content": ANALYST_SYSTEM},
            {"role": "user", "content": f"QUESTION: {question}\n\nEVIDENCE:\n{block}"}]

    async def analyst(i: int, ref: str):
        return await hedged(engine, f"analyst{i + 1}", [ref] + spare[i::ANALYSTS], msgs, emit,
                            role="analyst")

    got = [g for g in await asyncio.gather(*(analyst(i, r) for i, r in enumerate(picks))) if g]
    premium = [p for p in await premium_task if p]
    if not got and not premium:
        raise NoModelAvailable("no analyst could read the evidence")

    # 4. verifier: your most senior signed-in manager, else the best free judge
    await _emit(emit, {"type": "phase", "phase": "verifying"})
    answers = "\n\n".join([f"--- ANALYST {i + 1} ---\n{g[1].text}" for i, g in enumerate(got)]
                          + [f"--- {name.upper()} (searched the web itself) ---\n{t}" for name, t in premium])
    used = {g[0] for g in got} | {name for name, _ in premium}
    brief = f"QUESTION: {question}\n\nEVIDENCE:\n{block}\n\nANALYSTS:\n{answers}"
    final, writer = "", ""
    for boss in sorted(team, key=lambda m: m.rank)[:1]:
        await _emit(emit, {"type": "working", "id": "verifier", "worker": boss.name, "role": "verifier"})
        try:
            final, writer = await ask(boss, VERIFIER_SYSTEM + "\n\n" + brief), boss.name
            await _emit(emit, {"type": "delivered", "id": "verifier", "worker": boss.name,
                               "role": "verifier", "text": "verified", "seconds": None})
        except RuntimeError as e:
            await _emit(emit, {"type": "task_failed", "id": "verifier", "worker": boss.name,
                               "role": "verifier", "reason": str(e)})
    if not final:
        verifiers = [r for r in engine.judges() if r not in used] + engine.judges()
        done = await hedged(engine, "verifier", list(dict.fromkeys(verifiers + engine.chain(["reasoning"]))), [
            {"role": "system", "content": VERIFIER_SYSTEM}, {"role": "user", "content": brief}],
            emit, role="verifier")
        final, writer = (done[1].text, done[0]) if done else (
            (got[0][1].text, got[0][0]) if got else (premium[0][1], premium[0][0]))
    final += "\n\n**Sources**\n" + "\n".join(f"{e.n}. [{e.title}]({e.url}) — {e.source}"
                                            for e in evidence if e.url) if evidence else ""
    await _emit(emit, {"type": "final", "text": final, "writer": writer,
                       "models": sorted(used), "mode": "research"})
    return {"answer": final, "evidence": evidence, "queries": queries, "analysts": sorted(used)}

