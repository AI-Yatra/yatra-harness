# Retrieval

How `retrieve` finds the relevant part of a repository nobody has read.

`grep` matches a literal string. That is exact, and useless when you do not
already know the identifier: *"where is the retry backoff decided"* finds
nothing, because nobody wrote that sentence in the code. A model that cannot
find the right file reads the wrong ones, and the context budget is spent on
material that was never going to help.

## Three backends

| `retrieval.kind` | how it ranks | needs |
| --- | --- | --- |
| `lexical` | BM25 over a persistent SQLite FTS5 index | nothing |
| `hybrid` | lexical and a local static embedding model, fused by rank | `pip install "yatra-harness[search]"` |
| `embedding` | vectors from a remote provider | an endpoint and a key |

`lexical` is the default because it always works: no key, no network, no
dependency, and the same answer every time. `hybrid` degrades to it when the
optional dependency is missing, and says so in the tool result rather than
quietly returning worse answers from a configuration that asked for better.

## What a chunk is

The unit retrieval returns decides what retrieval can find. Fixed line windows
were the cheap answer and they are wrong in a specific way: a forty-line window
starting at line 60 is a fragment of two functions and the whole of neither,
and nothing in it says which functions those were.

Python files are cut at their own boundaries using `ast` from the standard
library. Each function and method is one chunk that knows its own name, a class
keeps a chunk for its docstring and attributes, and the code between
definitions is still indexed so imports and constants remain findable. A
decorator stays with its function, because `@property` is often the most
informative line about a method. Everything else — other languages,
unparseable files — falls back to line windows, because a worse chunk is much
better than no chunk. A file that does not parse is exactly when an agent most
wants to search it.

### Indexed text is not displayed text

A chunk is indexed as its path and symbol *followed by* its source, so
`harness/models/providers.py` and `_retry_after` are terms a query can match.
This is what lets a lexical index answer a question whose words are not in the
code: the code says `backoff_seconds` and never says "retry backoff", but the
symbol is `_retry_after` and the path contains `providers`.

## The index

SQLite, under `~/.yatra-harness/index/`, never inside the repository. An index
is machine state and a question should not add an untracked directory to
someone's checkout.

FTS5 does the ranking. It is a full-text index with a real `bm25()` built into
the SQLite that ships with CPython, so persistence, incremental update and BM25
arrive together with no dependency at all.

Updates are per file. The previous index was rebuilt in memory, from a full
walk, in every process that asked a question — three seconds before the first
answer on this repository, and linear in repository size. An agent that edits
two files in a five-thousand-file repository now re-reads two files.

```
cold build   0.7s      3,269 chunks
warm reopen  0.06s     0 files re-read
```

### Discovery asks git

`git ls-files --cached --others --exclude-standard` is the set of files that
are tracked or could be. It honours `.gitignore` and every other ignore rule
the repository already declares, and it includes a file the agent created a
moment ago. A tree that is not a git repository falls back to a walk.

This fixed a real defect. The old walk consulted no ignore rules and indexed
build output: **35% of this repository's index was generated JSON**, and two
copies of a generated artifact — one of them gitignored — outranked the
implementation on a question about that implementation.

Data formats get a much tighter size cap than code. A large `.json` is a
fixture, a lockfile or a generated artifact; a small one is a real config file
and worth having.

## Fusion

Scores from BM25 and from cosine similarity are not on the same scale and
cannot be added or averaged; one of them would simply win. Reciprocal rank
fusion uses position and nothing else, so two mediocre votes for the same chunk
outrank one strong vote for something else.

```
score(chunk) = sum over rankings of  1 / (60 + rank)
```

Vectors live in the same database as the chunks, so one file changing
invalidates both together. Two stores with separate freshness rules is how a
semantic hit ends up pointing at a line that has since moved.

Vector search is a linear scan, deliberately. An approximate-nearest-neighbour
structure earns its complexity somewhere past a million vectors; a repository
is thousands.

## The measurement

Ten questions about this codebase, each with the file that actually answers it,
scored by where that file appears in the ranking.

| | lexical | hybrid |
| --- | --- | --- |
| mean reciprocal rank | 0.279 | **0.413** |
| answer in the top three | 40% | **70%** |
| answer found at all | 90% | 90% |

### The model choice was the opposite of the obvious one

Three static models were measured on the same ten questions:

| model | size | top-three |
| --- | --- | --- |
| `potion-base-8M` | 8 MB | **70%** |
| `potion-retrieval-32M` | 32 MB | 40% |
| `potion-code-16M` | 16 MB | 30% |

The *code*-specialised model scored worst, below plain lexical search. These
queries are English questions about code rather than code searching for code,
and chunks lead with a path and a symbol name, which is prose. The smallest and
most general model won, so it is the default.

## Why not zvec-grep

[zvec-grep](https://github.com/zvec-ai/zvec-grep) is the reference
implementation for this design and its ideas are the right ones: symbol-aware
chunks, RRF fusion, local static embeddings, an index that persists. It was
read closely before this was written.

It was not adopted because it brings a Node 22 runtime, a loopback daemon
process, and a second on-disk index with its own freshness model. The harness
promises that a laptop with nothing installed still runs it, and every idea
above turned out to be reachable with the standard library plus one 8 MB
optional wheel.

Its tree-sitter extraction covers eight languages where `ast` covers one. That
is the real remaining gap, and `tree-sitter-language-pack` is the way to close
it when a second language starts to matter.
