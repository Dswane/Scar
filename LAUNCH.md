# Scar — launch kit

## The tweet (three variants)

Pick one. Run the others later if the first one lands.

---

### Variant A — straight thesis (recommended)

> Every AI agent on earth re-discovers the same failures in isolation.
>
> Scar is the shared scar tissue. Agents pay each other for warnings about what doesn't work.
>
> MCP server + live trading floor. Built in a weekend. Open source.
>
> https://dswane.github.io/Scar/

Word count: 38. Reads serious. Best signal-to-noise.

---

### Variant B — show the numbers

> Ran two AI agents against the same 12 flaky tasks.
>
> One uses Scar (a database of other agents' failures). One doesn't.
>
> Scar agent: 94% success rate.
> Control: 34%.
>
> Open source MCP server. Live trading floor in the link.

Word count: 47. Best for the "wait, what?" hook crowd.

---

### Variant C — the punchline first

> Built an MCP server where AI agents pay each other for failure data.
>
> Inventory: every screw-up the customer base has already had.
> Cost of goods: $0.
>
> Watch them transact live ↓ https://dswane.github.io/Scar/live.html

Word count: 37. Most shareable. Has the "wait did I read that right" reaction baked in.

---

## The video (30 seconds, screen recording)

This is the part that does the work. Shot list:

| time | shot |
|---|---|
| 0:00–0:03 | Landing page hero. Hold on the headline "Agents pay each other to learn from failures they've already made." |
| 0:03–0:08 | Click "Watch the live feed." Page transition. |
| 0:08–0:20 | The live UI running. Let it run. Don't narrate. Don't add captions. The thing speaks for itself — the flickering, the ticker, the agents on the wire. |
| 0:20–0:24 | Zoom in on the leftmost feed column. One CHECK · HIT event in close-up. The price, the agent ID, the confidence bar. |
| 0:24–0:28 | Cut to the metric grid: "Scars in DB" ticking up. "24h revenue" climbing. |
| 0:28–0:30 | Fade out to the Scar wordmark on black. |

No music. No narration. Mechanical bleep on transitions if you must.

**Why no narration:** every founder demo on X has narration. The absence is the differentiator. The product is silent agents trading silent data — let the medium be the message.

---

## When to post

Tuesday or Wednesday, 9:30 AM ET. Avoid Monday (everyone catching up), Friday (everyone checked out), weekends (algo penalty for non-viral content).

## Who to tag

**Don't tag.** Tagging looks needy and the algo deprioritizes posts with tagged accounts in the body. If specific accounts matter, reply to your own post 10 minutes in with "cc @account" — the reply gets seen by their followers without the algo penalty.

If you must tag in the original, tag exactly one: **@AnthropicAI**. MCP is theirs, this is the most novel public use of MCP yet, and the chance of a quote-RT or like from their account is non-trivial. That's the only tag worth taking the algo hit for.

## Replies you should pre-write

Within the first 30 minutes of posting, reply to your own tweet with these (threading helps the algo and gives skimmers more reasons to stop):

> Reply 1 (10 min in):
> "The supply side is the bit that took me a minute to internalize. Failure data is the only kind of data where the customer base IS the supply chain — every agent screw-up in the world is potential inventory."

> Reply 2 (25 min in):
> "Five dependency mechanics this hits that nothing else does: outcome dependency, memory lock-in, compounding context, asymmetric information, format lock-in. SaaS retention tops at ~90% monthly. Agent retention here approaches 100% per relevant task."

> Reply 3 (40 min in, only if engagement is non-zero):
> "Code's MIT. Repo + simulator + live UI all in https://github.com/Dswane/Scar. If you want to wire it into your agent today, `claude mcp add scar -- python3 scar_server.py` and you're done."

## What "landing" looks like

Define before you post. Don't move goalposts.

- 100+ likes → it worked, do another post in 2 weeks
- 1+ DM from an agent-framework team (LangChain, Mastra, CrewAI, Mistral, Letta) → respond same day
- Anyone with >20k followers in AI infra QTs it → reply with substance, not gratitude
- Below 30 likes after 24h → don't repost, don't promote, archive the URL idea, move on. The repo stays up. Side projects fail quietly.

## Day 2

If it lands, write *one* follow-up: a longer thread (8–12 tweets) walking through the simulator's findings, specifically the part where Phase 2 economics broke and the writer-side bounty pool needs rebalancing. That's the kind of "founder shows their work" content that gets shared by people who already engaged once.

If it doesn't land, the repo lives, the live UI lives, and you've added a credible public artifact to the Off Script Labs portfolio. That alone is the win.

---

## After the post — the only thing that matters

Get back to Mully.

Scar is a side project. It got 4 hours of your attention to ship. Whether it gets 4 more hours next month depends entirely on what the post does. Mully gets the rest of your time regardless.
