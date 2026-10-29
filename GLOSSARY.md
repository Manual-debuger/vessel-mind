# vessel-mind

A standalone agent runtime. It answers over a public corpus with citations, uses tools, and asks a
human before any side effect. Vessel will later consume it over the event protocol.

## Language

### Conversation

**Session**:
One conversation between a client and the runtime, identified by an id the server creates. Its
history survives a server restart.
_Avoid_: conversation, chat, thread

**Turn**:
One user message and everything the runtime does to answer it, up to `done` or `error`. A
session runs at most one turn at a time.
_Avoid_: request, exchange, run

### Routing

**Fast path**:
The route for a turn that a small model can answer, usually because retrieval already supplied
the context.
_Avoid_: local path, cheap path

**Slow path**:
The route for a turn that needs multi-step tool use or real reasoning, served by a
frontier-class model.
_Avoid_: cloud path, Claude path, hosted path

**Route**:
The router's decision for one turn: fast path or slow path.
_Avoid_: needs_tools, classification

**Costly misroute**:
A turn routed to the fast path whose answer scores clearly lower than the slow-only answer to
the same question.
_Avoid_: wrong route (too vague: a slow-path mistake costs money, not quality)

**Configuration**:
A named, fixed pair of models for the fast path and the slow path: `hosted`, `local` or
`routed`. Every eval result belongs to exactly one configuration.
_Avoid_: profile, setup, mode

### Knowledge and evaluation

**Corpus**:
The public documents the runtime retrieves from, each source pinned to one commit. A change to
the corpus is a new pin, never a silent edit.
_Avoid_: knowledge base, dataset, docs

**Golden set**:
The fixed list of eval questions with their expected sources, key points and expected route,
frozen before the first end-to-end run.
_Avoid_: test set, benchmark, eval data

**Unanswerable question**:
A golden-set question whose answer is not in the corpus. The correct answer says the documents
do not cover it.
_Avoid_: trick question, negative

**Answer score**:
How well one answer covers its question's key points, from 0 to 1. Citation correctness is
reported beside it, not inside it.
_Avoid_: quality score, grade

**Judge**:
The model that decides, for each key point, whether an answer covers it. It is replaceable and
is chosen separately from the configuration being measured.
_Avoid_: grader, evaluator

**Human grade**:
The author's own yes/no, per key point, on one sampled answer, made without seeing the judge's.
Human grades measure how often the judge agrees with the author; they are not answer scores.
_Avoid_: label, annotation

**Row**:
One line of the end-to-end table: a configuration with its router on, or with it off, where
every turn takes the slow path (`slow-only` is `hosted` with the router off).
_Avoid_: run, variant

**List-price cost**:
The cost a run would have at the provider's paid list price on a stated date. Nobody paid it;
it exists so configurations can be compared on cost.
_Avoid_: cost, spend, price

### Side effects

**Proposal**:
A request from the loop to run one write or irreversible tool call. It waits for a human
decision and executes at most once.
_Avoid_: pending action, request, draft

**Approval**:
The explicit human decision that executes a proposal. It is the only way a write or
irreversible tool runs.
_Avoid_: confirmation, consent

**Demo mode**:
The server setting for the public hostname. Read tools execute and proposals are shown, but every
approval is refused.
_Avoid_: read-only mode, public mode
