<!-- TEMPLATE -->
<!--
  This is your research brief. The Librarian judges every paper against it,
  so it is the single most important file in this repo. Fill in each section,
  then DELETE THE "TEMPLATE" MARKER on the first line. Until you do, the
  Librarian will not catalog anything (so it never spends tokens on a
  placeholder).

  Sections whose names the Librarian parses (keep the heading text):
    ## Project, ## Key claims, ## Citation gaps, ## Topic areas, ## Tags
  Every other section is passed to the models as free text.

  Editing this file later is fine: papers judged low-relevance (0-1) under the
  old version are re-triaged automatically. Keep the triage-relevant parts
  (Project, Key claims, rubric) short: they are sent with every paper.

  The examples below use a made-up project on urban heat islands. Replace them.
-->

# Research brief

## Project

One paragraph: what you are working on, the question you are trying to
answer, and what would count as an answer. Name your method, setting and
data, so the Librarian can tell a comparable paper from a merely related one.

*Example: My dissertation asks whether street-tree canopy reduces night-time
heat-related hospital admissions in mid-sized US cities, independent of
income. I use satellite land-surface temperature, municipal tree inventories
and admissions data for 12 cities, 2010-2022, with a spatial
difference-in-differences design.*

## Key claims

The claims your work makes or depends on, one bullet each, with a short id.
Papers are tagged with the ids they bear on, and a paper that directly
contradicts one is always flagged. Two to five is a good number.

- **A**: *Tree canopy lowers night-time land-surface temperature at block scale.*
- **B**: *The cooling effect persists after controlling for income and impervious surface.*
- **C**: *Night-time temperature, not daytime peak, drives heat-related admissions.*

## Relevance rubric

How to score a paper 0-3 for *your* work. Score the role the paper would play,
not how important its topic is in general.

- **3: core.** You build on it, compare against it, adopt its method, or must
  answer it. Any paper that contradicts a key claim is a 3. Read in full.
- **2: supporting.** Cited in the literature review with some influence on
  the design or the argument. Read in full when a PDF is available.
- **1: background.** Cited in passing, if at all. Abstract summary only.
- **0: off-topic.** One-line summary, no tags.

*Example adjustments: subtract 1 for daytime-only studies with no night-time
measure; landmark remote-sensing method papers I only use as tools are a 1.*

## Note conventions

Anything the Librarian should do when writing a full note on a paper:
terminology to use, confounds to always record, things it must not overstate
about your own work.

- Separate what the paper claims, what it means for this project, and what
  it does not establish. Give a page number for every quoted number.
- *Always record the spatial resolution and whether temperature is air or
  land-surface.*

## Citation gaps

Statements in your draft that still need a citation. Each bullet becomes a
gap id (G1, G2, ...) and the Librarian lists candidate papers for each.
Delete the examples; an empty section is fine.

- *The urban heat island is stronger at night than during the day.*
- *Income and canopy cover are correlated in US cities.*

## Topic areas

Coarse labels for grouping papers, as `slug: description`. An `other` area is
always added.

- remote_sensing: satellite or aerial temperature measurement
- epidemiology: health outcomes and heat exposure
- urban_forestry: tree canopy, green infrastructure
- methods: causal inference and spatial statistics

## Tags

Tags the Librarian may *suggest* (you accept or ignore them in the UI). It
never invents tags outside this table. Use your existing tag names.

| Tag | Use when the paper… |
|---|---|
| Heat & Health | links heat exposure to a health outcome |
| Canopy Cooling | measures cooling from trees or vegetation |
| Methods | is mainly about a statistical or measurement method |
| Review | is a survey or review article |
