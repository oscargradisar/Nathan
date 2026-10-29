"""Research mode: answer niche questions from real sources, not from model memory.

Models alone confidently invent niche facts. So Nathan first gathers evidence from free,
official, keyless sources, then several models answer *from that evidence* with
citations, and a verifier cross-checks them into one cited answer with a confidence level.

  1. scout     a quick model turns the question into a few search queries
  2. search    Wikipedia, DuckDuckGo instant answers, Stack Overflow, OpenAlex (papers),
               arXiv, Hacker News and Marginalia web search, all at once; linked pages are
               read in full, keeping the passages that match the question
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
from nathan.tools.fetch import html_to_text, links, read_page, read_page_html

Emit = Callable[[dict[str, Any]], Awaitable[None] | None]
# Wikimedia rejects clients whose User-Agent lacks a real https:// contact URL
UA = {"user-agent": "Nathan/1.0 (https://github.com/oscargradisar/Nathan) httpx2"}
SNIPPET = 1800        # characters of text kept per source item
MAX_EVIDENCE = 16     # items handed to the analysts
ANALYSTS = 4
PREMIUM_TIMEOUT_S = 180.0  # a premium researcher that takes longer is left out
SOURCE_BUDGET_S = 25.0  # rendered pages take a few seconds each  # a source that takes longer keeps what it found so far

SCOUT_SYSTEM = """You plan web research. Turn the question into 2-4 short search queries
(keywords, not sentences) that would find the answer in Wikipedia, papers or forums.
Also list up to 3 exact web pages likely to hold the live answer (official sites,
leaderboards, rankings, docs, stats pages) if you know their addresses.
Reply with ONLY JSON: {"queries": ["...", "..."], "urls": ["https://..."]}"""

ANALYST_SYSTEM = """You are a research analyst. Answer the question using the numbered
EVIDENCE below. Cite every factual claim like [3]. If the evidence doesn't cover something,
say so plainly ("not in the sources") and only then add what you know, marked as unsourced.
Be specific: names, numbers, dates. No filler."""

VERIFIER_SYSTEM = """You are the verifier. Several analysts answered the same question from
the same numbered EVIDENCE. Write the final answer:
- Keep only claims supported by the evidence or agreed by several analysts; cite them like [2].
- Where analysts disagree, say so and say which the evidence supports.
- Analysts marked "searched the web itself" read live pages you can't see. For current facts
  (rankings, prices, who holds a title now), agreement between them is strong evidence.
- End with a line "Confidence: high|medium|low — <why>".
Answer the person directly; don't mention analysts."""


class Queries(BaseModel):
    queries: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)


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


def passages(text: str, query: str, budget: int = SNIPPET) -> str:
    """The paragraphs of a long page that best match the query, kept in page order.
    Specifics (dates, names) usually live deep in an article, not in its intro."""
    terms = {w for w in re.findall(r"\w+", query.lower()) if len(w) > 2}
    paras = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n|\n(?=[A-Z=])", text)]
    paras = [p for p in paras if len(p) > 40]
    if not paras:
        return _clean(text)
    score = [sum(t in p.lower() for t in terms) for p in paras]
    ranked = sorted(range(len(paras)), key=lambda i: -score[i])
    keep, used = set(), 0
    for i in ranked:
        if keep and (score[i] == 0 or used + len(paras[i]) > budget):
            continue  # only matching paragraphs that fit; the best one always makes it
        keep.add(i)
        used += len(paras[i])
    return " … ".join(paras[i] for i in sorted(keep))[:budget]


# ---- sources: each returns [(title, url, text)] ----------------------------------------

WIKI = "https://en.wikipedia.org/w/api.php"


async def wikipedia(http, q: str) -> list[tuple[str, str, str]]:
    """Search, then read the top articles in full and keep the passages that match."""
    hits = (await http.get(WIKI, params={"action": "query", "list": "search", "srsearch": q,
                                         "srlimit": 2, "format": "json"})).json()
    out = []
    for h in (hits.get("query") or {}).get("search", []):
        title = h["title"]
        pages = (await http.get(WIKI, params={"action": "query", "prop": "extracts", "explaintext": 1,
                                              "titles": title, "redirects": 1, "format": "json"})
                 ).json().get("query", {}).get("pages", {})
        text = next((p.get("extract", "") for p in pages.values()), "")
        if text:
            out.append((title, "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_")),
                        passages(text, q)))
    return out


async def marginalia(http, q: str) -> list[tuple[str, str, str]]:
    """Marginalia Search: a free, official web search API (public key, rate-limited) that
    is good at niche, non-commercial pages. The top results are read and trimmed to passages."""
    j = (await http.get(f"https://api.marginalia.nu/public/search/{quote(q)}", params={"count": 4})).json()
    out = []
    for r in j.get("results", [])[:3]:
        text = r.get("description", "")
        _, page = await read_page(r["url"], http)
        text = passages(page, q) or text
        if text:
            out.append((r.get("title") or r["url"], r["url"], text))
    return out


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
            _, page = await read_page(h["url"], http)
            text = passages(page, q) or text
        if text:
            out.append((h.get("title") or url, url, text))
    return out


SOURCES = {"Wikipedia": wikipedia, "Web (Marginalia)": marginalia, "DuckDuckGo": duckduckgo,
           "Stack Overflow": stackoverflow, "OpenAlex": openalex, "arXiv": arxiv, "Hacker News": hackernews}


async def gather_evidence(queries: list[str], emit: Emit | None = None,
                          http: httpx.AsyncClient | None = None, urls: list[str] | None = None,
                          question: str = "") -> list[Evidence]:
    own = http is None
    http = http or httpx.AsyncClient(timeout=12, headers=UA, follow_redirects=True)
    found: dict[str, list[tuple[str, str, str]]] = {}
    sources = dict(SOURCES)
    if urls:
        async def pages(_http, _q, _urls=tuple(urls)) -> list[tuple[str, str, str]]:
            if _q != queries[0]:
                return []  # read each page once, not once per query
            terms = {w for w in re.findall(r"\w+", " ".join(queries).lower()) if len(w) > 3}
            first = await asyncio.gather(*(read_page_html(u, _http) for u in _urls))
            follow: list[str] = []
            for u, (_, text, raw) in zip(_urls, first, strict=True):
                if len(text) < 1500:  # thin page (a menu): follow on-site links that match the question
                    host = u.split("/")[2] if "//" in u else ""
                    follow += [h for h, label in links(raw, u) if host in h and h not in _urls and h not in follow
                               and any(t in (h + " " + label).lower() for t in terms)][:2]
            follow = follow[:3]
            second = await asyncio.gather(*(read_page_html(u, _http) for u in follow))
            # live pages keep their top: ranking tables hold names and numbers, not query words
            return [(u.split("//", 1)[-1][:80], u, text[:SNIPPET * 3])
                    for u, (_, text, _raw) in zip(list(_urls) + follow, list(first) + list(second), strict=True)
                    if len(text) > 200]
        sources = {"Live pages": pages, **sources}

    async def one(name: str) -> None:
        await _emit(emit, {"type": "working", "id": "src:" + name, "worker": name, "role": "source"})
        items: list[tuple[str, str, str]] = []

        async def search(q: str) -> None:
            try:
                items.extend(await sources[name](http, q))
            except (httpx.HTTPError, ValueError, ET.ParseError, KeyError):
                pass  # one source failing must not sink the research

        # queries in parallel, and a slow source keeps what it found by the deadline
        tasks = [asyncio.create_task(search(q)) for q in queries]
        _, late = await asyncio.wait(tasks, timeout=SOURCE_BUDGET_S)
        for t in late:
            t.cancel()
        found[name] = items
        await _emit(emit, {"type": "delivered" if items else "task_failed", "id": "src:" + name,
                           "worker": name, "role": "source", "seconds": None,
                           "text": "\n".join(f"• {t}" for t, _, _ in items[:6]) or "nothing found",
                           **({} if items else {"reason": "nothing found"})})

    try:
        await asyncio.gather(*(one(n) for n in sources))
    finally:
        if own:
            await http.aclose()
    # Drop off-topic hits (a paper that only shares the word "game"), then round-robin
    # across sources so one chatty source can't crowd out the rest
    terms = {w for w in re.findall(r"\w+", " ".join(queries).lower()) if len(w) > 3}
    need = min(2, len(terms))
    # names the question is about ("Rhythia"): evidence must mention one of them
    names = {w.lower() for w in re.findall(r"(?<!^)(?<![.?!]\s)\b[A-Z][a-zA-Z0-9]{2,}", question or "")}

    def relevant(item: tuple[str, str, str]) -> bool:
        if item[1] in (urls or []):
            return True  # pages the scout picked on purpose always count
        blob = (item[0] + " " + item[2]).lower()
        if names and not any(n in blob for n in names):
            return False
        return sum(t in blob for t in terms) >= need

    found = {name: [i for i in items if relevant(i)] for name, items in found.items()}
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
        text = await ask(m, PREMIUM_BRIEF.format(q=question), web=True, timeout=PREMIUM_TIMEOUT_S)
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
        urls = [u for u in plan.urls if u.startswith(("http://", "https://"))][:3]
    except (NoModelAvailable, TimeoutError):
        queries, scout, urls = [question[:200]], "keyword fallback", []
    await _emit(emit, {"type": "delivered", "id": "scout", "worker": scout, "role": "scout",
                       "text": "\n".join(queries + urls), "seconds": None})

    # 2. search
    await _emit(emit, {"type": "phase", "phase": "searching"})
    evidence = await gather_evidence(queries, emit, http, urls, question)
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
    for boss in managers.lead_order(team)[:1]:
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

