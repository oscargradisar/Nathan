// Nathan's running commentary. Templates, not model calls: the jokes are free and so is
// the quota they don't use. Each returns [text, tone] where tone styles the log line.
import { pick, short } from "./core.js";

const BOOT = [
  "Nathan online. {n} models on staff, none of them paid, all of them confident.",
  "Systems nominal. Which is what systems say right before they aren't.",
  "Ready. Ask something; the models are getting bored and bored models hallucinate.",
  "Online. Running entirely on free tiers and the goodwill of strangers' GPUs.",
];

const T = {
  start: ["Request received. Assembling a committee, as is tradition.",
    "On it. Several models are about to have opinions about this.",
    "Accepted. Somewhere a free tier just felt a disturbance."],
  routed: {
    single: ["Routing: one fast model. Not everything needs a meeting."],
    race: ["Routing: race. First correct answer wins, everyone else is politely ignored."],
    vote: ["Routing: council. Several models answer blind; a judge reconciles their egos."],
    swarm: ["Routing: swarm. Everyone answers. This is either wisdom of crowds or a group chat."],
    critique: ["Routing: critique. One writes, one criticises, one fixes. Workplace realism."],
    workflow: ["Routing: workflow. A manager is about to delegate, which is the whole job of a manager."],
    research: ["Routing: research. Actual sources this time. The models have been told to cite or be quiet."],
    code: ["Routing: programming. ChatGPT is putting on its manager lanyard."],
    review: ["Performance review. The models have been told it's 'just a conversation'. It is not."],
    agent: ["Routing: agent. It may touch files. Supervised, like a toddler with scissors."],
  },
  plan: ["Manager split this into {n} parts and assigned them. It did none of the parts. Classic.",
    "{n} parts, {w} workers. The org chart writes itself.",
    "Plan ready: {n} parts. The manager would like credit for this."],
  working: ["{m} is on {id}.", "{m} picked up {id}.", "{id} → {m}."],
  backup: ["{slow} is taking its time on {id}. {m} has quietly been asked to do the same thing. Nobody tell {slow}.",
    "{id}: {slow} is thinking very hard. {m} is now also thinking. Whoever finishes first gets the glory.",
    "Hedging {id}: {m} joins because {slow} is buffering its personality."],
  reassigned: ["{from} didn't deliver {id} ({why}). {m} covers. This is what the leaderboard is for.",
    "{id}: {from} went quiet ({why}). Handed to {m}. Noted on {from}'s permanent record."],
  delivered: ["{m} delivered {id} in {s}s.", "{id} done by {m} ({s}s).", "{m}: {id} complete, {s}s."],
  failed: ["Nobody could do {id}. The rest of the team will cope, as teams do."],
  redo: ["Reviewer rejected {ids}. Redoing with someone better rated. Second opinions, now with consequences.",
    "{ids} scored below 5. Reassigning. The original worker has been invited to reflect."],
  writing: ["Writer is stitching it together into something a human can read."],
  reviewing: ["Reviewer is comparing what you asked for with what you got. Brutally, one hopes."],
  review: ["Review in: {sum}", "Reviewer says: {sum}"],
  ratings: ["Elo updated. {best} climbs. {worst} will be thinking about this.",
    "Ratings applied. {best} is insufferable now."],
  prayer: ["The team would like to consult a higher power. It's Claude. You'll have to carry the message.",
    "A prayer has been drafted. Nathan does not have Claude's number, so you're the courier."],
  blessed: ["Claude has spoken. The answer has been revised accordingly. Theology settled."],
  final: ["Answer ready: {k} model(s) contributed. It's models all the way down.",
    "Done. Assembled from {k} model(s), reviewed by more models. Peer review, technically.",
    "Answer delivered. {k} model(s) were involved and none of them will remember it."],
  lane: ["{m} has answered. Its confidence is unrelated to its accuracy."],
  judge: ["{m} is judging. It has been told to be fair, which it will interpret loosely."],
  error: ["Something broke: {msg}", "Not ideal: {msg}"],
  stepdone: ["{role} done: {m} ({s}s)."],
  stepwork: ["{role}: {m} is working."],
};

const fill = (s, v) => s.replace(/\{(\w+)\}/g, (_, k) => v[k] ?? "");

export function boot(n) { return [fill(pick(BOOT), { n }), "quip"]; }

export function quip(ev) {
  const m = short(ev.worker || ev.model || "");
  switch (ev.type) {
    case "routed": return [pick(T.routed[ev.pattern] || ["Routing: " + ev.pattern + "."]) + (ev.reason ? ` (${ev.reason})` : ""), "quip"];
    case "plan": return [fill(pick(T.plan), { n: ev.tasks.length, w: new Set(ev.tasks.map((t) => t.worker)).size }), "quip"];
    case "working": return ev.role === "patient" ? null : [fill(pick(T.working), { m, id: label(ev) }), "sys"];
    case "backup": return [fill(pick(T.backup), { m, slow: short(ev.for), id: label(ev) }), "warn"];
    case "reassigned": return [fill(pick(T.reassigned), { m, from: short(ev.from), id: label(ev), why: why(ev.reason) }), "warn"];
    case "delivered": return ev.role === "patient" ? null
      : [fill(ev.seconds != null ? pick(T.delivered) : "{m} delivered {id}.", { m, id: label(ev), s: ev.seconds }), "good"];
    case "task_failed": return ev.role === "patient" ? null : [fill(pick(T.failed), { id: label(ev) }), "bad"];
    case "redo": return [fill(pick(T.redo), { ids: ev.ids.join(", ") }), "warn"];
    case "phase":
      if (ev.phase === "writing") return [pick(T.writing), "sys"];
      if (ev.phase === "reviewing") return [pick(T.reviewing), "sys"];
      return null;
    case "review": return [fill(pick(T.review), { sum: ev.summary }), "sys"];
    case "ratings": {
      const e = Object.entries(ev.changes).sort((a, b) => b[1] - a[1]);
      if (e.length < 2) return null;
      return [fill(pick(T.ratings), { best: short(e[0][0]), worst: short(e[e.length - 1][0]) }), "quip"];
    }
    case "prayer": return [pick(T.prayer), "quip"];
    case "sources": return [`${ev.items.length} sources found. Nobody gets to make things up now. Officially.`, "quip"];
    case "saved": return [`Saved ${ev.files.length} file(s). It compiles in spirit.`, "good"];
    case "blessed": return [pick(T.blessed), "good"];
    case "lane_done": return ev.ok ? [fill(pick(T.lane), { m }), "sys"] : null;
    case "judge_start": return [fill(pick(T.judge), { m }), "sys"];
    case "step":
      if (ev.status === "working") return [fill(pick(T.stepwork), { role: cap(ev.role), m }), "sys"];
      if (ev.status === "done") return [fill(pick(T.stepdone), { role: cap(ev.role), m, s: ev.seconds ?? "–" }), "good"];
      return null;
    default: return null;
  }
}

export const startLine = () => [pick(T.start), "quip"];
export const finalLine = (k) => [fill(pick(T.final), { k }), "good"];
export const errorLine = (msg) => [fill(pick(T.error), { msg }), "bad"];

function label(ev) {
  return { writer: "the write-up", reviewer: "the review", source: `the ${ev.worker} search`, scout: "the search plan",
    verifier: "the fact-check", "premium researcher": "web research" }[ev.role] || ev.id;
}
function cap(s) { return s ? s[0].toUpperCase() + s.slice(1) : s; }
function why(r) {
  return { QuotaExceeded: "free limit hit", ProviderDown: "no answer", ModelGone: "model retired",
    AuthError: "key refused", "empty answer": "said nothing" }[r] || r;
}
