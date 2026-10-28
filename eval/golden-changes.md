# Changing the golden set

The golden set is frozen. It is committed alone, in its own commit,
before the first end-to-end eval run (spec `a-agent-track.md` §5.8,
ticket #12). Every later change follows these rules.

1. **Fix only real errors.** A real error is one of these:
   - an expected source path that does not exist at the corpus pin, or
     that does not contain the answer;
   - a key point that the pinned documents contradict or do not support;
   - an `expected_route` that does not follow the route definition in
     spec §5.7;
   - an "unanswerable" question that the pinned corpus does answer;
   - a typo that changes the meaning of a question.

   A question that a configuration answers badly is not an error. Do not
   reword, replace, add or remove a question to change a score.

2. **Say why in the commit message.** Name each question id that
   changes, the error, and the evidence (for example the path and
   heading at the pin). Commit the change alone, with no code change in
   the same commit.

3. **Trigger a full re-run.** After the change, re-run the retrieval
   ablation and every end-to-end configuration that ran before. Results
   from before the change are not compared with results after it.

## How the Traditional Chinese questions were made

At the author's request, an agent drafted the Traditional Chinese
questions (zh-01 to zh-10) directly in Chinese, not as translations of the
English questions. The author verified each one before the freeze; each
is marked `drafted_by: agent` and `reviewed_by_author: true`. Ticket #12
and spec §5.8 first said the author writes the Chinese questions; both now
describe this method.

## Corpus changes

A corpus change (a new pin in `eval/corpus.yaml`) is a separate change
with its own eval re-run. When a new pin moves or removes an expected
source, fix the affected questions under rule 1.
