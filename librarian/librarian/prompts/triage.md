TASK: triage this paper for relevance to the project, using the relevance
rubric in the brief. Score the role the paper would play in the project;
a citation gap it fills goes in `gaps` and does not raise the score.

What you have: {have}

Fill in:
- relevance: 0–3 per the rubric.
- confidence: 0–1. Use below 0.6 if you are working from a citation only,
  or if the excerpt is too garbled or short to judge.
- claims: ids of the project's key claims the paper bears on (often none).
- areas: one or more topic areas.
- gaps: ids of citation gaps this paper could fill (often none). Only list a
  gap if the paper would actually be cited for that statement.
- suggested_tags: from the allowed list only; usually 1–4.
- contradicts: empty string, or one or two sentences on exactly what the paper
  says that DIRECTLY runs against one of the key claims. Mere tension or
  different assumptions is not a contradiction: mention that in `why`
  instead and leave this empty.
- summary: 2–4 sentences on what the paper does and finds. For relevance 0–1,
  this is a summary of the abstract and nothing more.
- why: one sentence on why it matters (or doesn't) for this project,
  specifically.

PAPER
Title: {title}
Authors: {authors}
Year / venue: {year} {venue}
Citation string: {citation}

{body}
