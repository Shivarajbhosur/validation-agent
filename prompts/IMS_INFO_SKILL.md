---
name: ims-hierarchy
description: 'Foundational IMS hierarchy knowledge shared by all IMS DL/I → ExperLib conversion tasks, and a router to the specialized skills. Use when: you need to understand the IMS physical vs logical DBD, SSA structure, PCB view, concatenated keys, or where the SSAB.OX IMS metadata lives; or when deciding which specialized conversion skill to apply (read vs delete). For a specific read call use select-ims. For DELETE/REPL use the (planned) delete-ims.'
argument-hint: 'Ask about IMS hierarchy/DBD/SSA concepts, or which conversion skill to use'
---

# IMS Hierarchy — Foundations & Skill Router

Shared knowledge for converting IMS DL/I (`CBLTDLI`) calls to ExperLib. Read this
first, then jump to the specialized skill for your operation.

## Specialized skills

| Operation | Skill | Status |
|-----------|-------|--------|
| Read (`GU`,`GN`,`GNP`,`GHU`,`GHNP`) → `SELECT` | **`select-ims`** | Available |
| Delete (`DLET`) + logical-relationship cascade | **`delete-ims`** | Planned |
| Replace / Insert (`REPL`,`ISRT`) | *(future)* | Planned |

Each specialized skill reuses the concepts below and adds operation-specific rules.

## Core mental model

IMS is a **hierarchical** database. An application reads it through a **PCB** that
exposes a **logical** database (a reshaped view over one or more physical
databases). A program navigates with **SSAs** (Segment Search Arguments), one per
hierarchy level, qualifying the path down to a target segment.

**The qualifying SSAs above the target are PATH NAVIGATION, not table lookups.**
When flattened to relational SQL they become simple `WHERE` filters on the target
segment's own table — only for keys that table actually contains. Parent SSAs
whose columns don't exist in the target table are dropped.

## Physical vs logical DBD

Two DBD flavours, both mandatory to read:

- **Logical DBD** (`ACCESS=LOGICAL`, `DATASET LOGICAL`; file `<db>L*.DBD`, e.g.
  `JAHAL00P.DBD`). Defines what the program actually sees. Segment names in the
  program's SSAs are **logical** names. Each maps to physical segment(s) via
  `SOURCE=`:
  - One part `SOURCE=((JPOSS021,,JPOSF00P))` → passthrough to physical `JPOSS021`.
  - Two parts `SOURCE=((JAHAS010,,JAHAF00P),(JPOSS001,KEY,JPOSF00P))` → a
    **logical relationship**: first = physical data/key segment, second (`KEY`) =
    the paired/destination segment whose key is concatenated in.
- **Physical DBD** (`ACCESS=(HDAM/HIDAM,…)`; file `<db>F*.DBD`, e.g.
  `JAHAF00P.DBD`). Ground truth for storage: `SEGM` with `PARENT=`, `FREQ=`,
  `BYTES=`; each segment's key is the following `FIELD NAME=(key,SEQ,U)` line.
  - `PARENT=` → hierarchy path.
  - `FREQ=` → children per parent (`FREQ=1` ⇒ one row).
  - `SEQ,U` → unique sequence key (a unique ancestor key can make parent SSAs
    droppable path-navigation).

Always resolve SSA names through the **logical** DBD first, then use the
**physical** DBD for hierarchy/cardinality/uniqueness.

## SSA anatomy

An SSA qualifies one hierarchy level: `segmentName (fieldName operator value)`,
e.g. `SSA-JAHAS003-ORDERNR-EQ`. Operators: `EQ` (`=`), `GT` (`>`), `GE`, `LT`,
`LE`. An **unqualified** SSA (segment name only) names the target without a
predicate.

## Concatenated keys

A single IMS key field can concatenate several logical values that map to
multiple SQL columns, e.g. `ORDPOSNR` X(14) = `CustomerOrderNumber`(10) +
`POSNR`(4). Split with `Subfield(field, offset, length)` (0-based, right-trimmed)
when the `.record` stores them as separate `keyNumber` sub-columns; use the whole
value when the `.record` stores one column.

## Where the SSAB.OX metadata lives

Use paths relative to the repository root (`<repo-root>`) for files in the
checkout. In this repository, the logical and physical DBD files are stored in
`<repo-root>/db/ims/DBD`; use that as `<ims-dbd-root>`. This keeps the paths
portable across machines and checkout locations.

| Artifact | Location |
|----------|----------|
| Source COBOL programs | `<repo-root>/legacy/COB/<PGM>.COB` |
| Logical + physical DBDs | `<repo-root>/db/ims/DBD/*.DBD` |
| Data dictionary (field → Table/Column) | `<repo-root>/db/ims/DBD/DBDICT.CSV` |
| Record definitions (`table=`, keys, parents) | `<repo-root>/db/ims/DBD/<Segment>.record` |
| Segment key summary | `<repo-root>/conversion/DBD.CSV` |

DBDICT.CSV header (key columns: `Table` col 20, `Column` col 21):
```
DBID,TblCode,File,Copybook,Field,Level,Type,Offset,Len,Pic,Occur,Digits,Dec,
Discrete,Redef,Issue,Soltn,Date Type,Map,Table,Column,SQL Type,SpFmt,Nulls,
Default,Scan,Cond,Seq
```

## ExperLib runtime (shared)

The target framework is ExperLib. A record area is a `SqlRecord` bound to a table
(`mainSchemaTable`). Common entry points across operations: `Select`, `Fetch`,
`FetchOne`, `SelectChildren`, `ReadByKey`, `ReadOne`, and the `Subfield` string
helper. Operation-specific APIs are documented in each specialized skill.
