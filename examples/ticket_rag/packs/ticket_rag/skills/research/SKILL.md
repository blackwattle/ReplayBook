---
name: past-ticket-research
description: Find the earlier tickets that show how a problem like this one was handled, and cite them.
request_type: ticket_research
tools: ticket_get, search_tickets, find_similar_tickets, get_ticket, knowledge_search_tickets, knowledge_get_ticket, ticket_comment
---

# Past-ticket research

## When this applies
Any ticket, before anyone works it: has this happened before, and how was it solved?
`replaybook knowledge eval --agent ... --type ticket_research` scores what you cite against
the earlier tickets engineers linked to this one.

## Steps
1. Read the ticket with `ticket_get`.
2. Search with `search_tickets`, describing the problem in your own words: the system, the
   error message, the change asked for. If the first search finds nothing close, try two or
   three different ones (other words, the component, the error alone).
3. Open the most promising results with `get_ticket` to see how they ended.
   `find_similar_tickets` finds more like a ticket you already have.
4. Finish with exactly one `ticket_comment`. Its body starts with `Related:` and the ids of
   the earlier tickets that help, most relevant first, at most 10, then one line per ticket
   on how it was handled.

## Rules
- Cite only ids a tool returned to you. Never write an id you have not seen.
- If nothing relevant turns up, comment `Related: none` and say what you searched for.
- Change nothing: this is research. Do not ask the requester or hand the ticket back.
