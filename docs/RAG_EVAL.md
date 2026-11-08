# Retrieval evaluation

How the document Q&A (RAG) settings were chosen, and how well they work. Reproduce with:

```
cd backend
python -m pytest --runslow tests/test_rag_eval.py -s     # ~2 min: indexes ~820 passages with the real model
```

## Setup

* **Documents:** the three real files in `sample_templates/` (a 10-K filing as DOCX, the same company's financial
  statements as a 77-sheet legacy XLS, and an investor-presentation PDF): 823 passages.
* **Embeddings:** `BAAI/bge-small-en-v1.5` (384-d, local). Queries use bge's query instruction prefix.
* **Passages:** ~900 characters, each carrying an exact locator (`¶12-14`, `Table 3`, `Slide 4`,
  `Sheet 'x' rows 5-19`, `p.7`).
* **Ranking:** `0.85 × cosine + 0.15 × lexical overlap` over the top 24 vector hits, near-duplicates removed.
  The lexical term keeps exact figures findable (embeddings blur numbers).
* A question counts as *retrieved at k* when one of the top-k passages contains the expected fact (numbers compared
  ignoring thousands separators: `217778` in the sheet vs `217,778` in the filing).

## Results

| Set | Questions | recall@1 | recall@3 | recall@8 |
|---|---|---|---|---|
| Tuning (used to choose the settings) | 13 | 10 (77 %) | 11 | **13 (100 %)** |
| **Held-out** (written after freezing, evaluated once) | 6 | 4 (67 %) | 5 | **6 (100 %)** |

The right passage is always within the 8 given to the model, but only first about two times in three, which is why the
answer step must cite and why 8 passages (not 1) are supplied.

Lexical weight (tuning set; recall@8 / on-vs-off-topic score gap):

| weight | recall@8 | gap |
|---|---|---|
| 0.00 (pure embeddings) | 12 | 0.059 |
| **0.15 (chosen)** | **13** | **0.111** |
| 0.25 (initial guess) | 13 | 0.099 |
| 0.40 | 13 | 0.079 |

**Relevance cutoff:** lowest on-topic top-1 score 0.602; highest off-topic ("hello", "capital of France", …) 0.490.
The cutoff is 0.55; messages scoring below it are handled as ordinary chat instead of being "answered from the
documents".

## Known misses

* *"Where is the company headquartered?"* ranks 8th: the passage reads "ONE MICROSOFT WAY, REDMOND, Washington" and
  never uses the word "headquarters". Pure semantic gap; found only because 8 passages are supplied.
* *"What is the company's phone number?"* ranks 7th for a similar reason (a bare number line).
* The 0.55 cutoff has a margin of only 0.11 over the measured off-topic maximum; a larger evaluation set would
  tighten it. 19 questions over 3 documents is a small sample.

## Guarantees enforced in code (not just by prompting)

* Every `[n]` marker in an answer is checked against the passages actually retrieved; invented numbers are removed
  and can never become citations (`tests/test_rag_qa_graph.py`).
* An answer that uses documents but cites nothing is labelled as such instead of looking verified.
* "Not found" answers carry no citations.
* One conversation's passages are never retrievable from another (separate namespace per conversation).
* Retrieval or vector-store failure degrades to plain chat instead of failing the request.
