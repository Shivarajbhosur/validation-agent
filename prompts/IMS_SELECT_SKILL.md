---
name: select-ims
description: 'Specialize in reading the IMS hierarchy (physical + logical DBD) to produce a correct ExperLib SELECT query for a DL/I read call. Use when: converting/reviewing/fixing a GU/GN/GNP/GHU/GHNP read, answering "is this Select() right?", tracing an SSA path down the hierarchy to a single SQL table, diagnosing wrong tables/JOINs/OR-expansion/parameters in a generated .cs Select(), resolving paired/mutual logical relationships (both SOURCE= parts untagged), converting programs that reuse one shared dbIoarea-style object across sequential Select/Fetch calls, or collapsing COBOL GN retry-loops (FREQ=1 vs FREQ>1). Reads the logical DBD, physical DBD, DBDICT.CSV, .record files, DBD.CSV and the source COBOL. For DELETE/REPL use the (planned) delete-ims skill. See ims-hierarchy for shared foundations.'
argument-hint: 'Name the paragraph/segment or paste the generated Select() and the CBLTDLI read call'
---

# SELECT: IMS Hierarchy → ExperLib SELECT Query

> Part of the IMS-conversion skill hierarchy. Read **`ims-hierarchy`** first for
> shared concepts (physical vs logical DBD, SSA, PCB, concatenated keys). This
> skill specializes those foundations for **read (SELECT)** calls only. DELETE /
> REPL / ISRT belong to the (planned) **`delete-ims`** skill.

## Purpose

Given a DL/I read (`GU`, `GN`, `GNP`, `GHU`, `GHNP`), walk the IMS hierarchy from
the qualified SSAs down to the target segment and emit the **correct single-table
ExperLib SELECT**. The converter is frequently wrong about *which table*, *which
columns*, and *how many parameters* — this skill derives the right query from the
metadata rather than trusting the generated output.

## Golden Rule

**The qualifying SSAs above the target segment are PATH NAVIGATION, not separate
table lookups.** They pick which branch of the hierarchy to walk. Flattened to
SQL they become simple `WHERE` filters on the **target segment's own table** —
and only for the keys that table actually contains. You almost never need a JOIN,
and you never filter on a column that does not exist in the target table.

The returned data comes from the **target segment** — the one whose area is the
3rd argument of the `CBLTDLI` call.

## Pre-flight: Validate the required artifacts exist (DO THIS FIRST)

Before tracing anything, confirm the three human-supplied artifacts are actually
present in the workspace. **Never guess or fabricate DBD/program contents** — if a
file is missing, STOP and ask the user to upload it.

1. **Source program** — `<repo-root>/legacy/COB/<PGM>.COB` (or the `.cob` passed in).
   Locate the exact `CBLTDLI` call being converted.
2. **Physical DBD** — `<repo-root>/db/ims/DBD/<db>F*.DBD` (e.g. `JPOSF00P.DBD`,
   `JAHAF00P.DBD`) for **every** DBD referenced by the SSAs' segments.
3. **Logical DBD** — `<repo-root>/db/ims/DBD/<db>L*.DBD` (e.g. `JAHAL00P.DBD`) for the
   PCB the program uses. Required to resolve logical SSA names via `SOURCE=`.

How to check (adjust names to the program at hand):

```powershell
# Run from anywhere inside the repository.
$RepoRoot = (git rev-parse --show-toplevel).Trim()
 
# DBD files in this repository are stored under db\ims\DBD.
$ImsDbdRoot = Join-Path $RepoRoot 'db\ims\DBD'

# Program
Get-ChildItem -Recurse -Filter '<PGM>.cob' `
  -Path (Join-Path $RepoRoot 'conversion'), (Join-Path $RepoRoot 'legacy')
 
# Physical + logical DBDs (F = physical, L = logical)
Get-ChildItem -Path $ImsDbdRoot -Filter '<db>*.DBD'
```
 
If **any** of the three is missing, do not proceed. Ask the user explicitly, e.g.:
 
> I can't find the **logical DBD** (`<db>L*.DBD`) / **physical DBD**
> (`<db>F*.DBD`) / **program** `<PGM>.COB` in the configured workspace paths.
> Please provide the missing file or its location so I can trace the hierarchy
> accurately. Without it I would only be guessing.

Only once all three are confirmed present do you continue to the six inputs and
the procedure below.

## The Six Inputs You MUST Read

Never answer from the COBOL alone. Cross-check all six. **Both DBDs are
mandatory**: the program's SSAs use *logical* segment names, so you must map them
through the **logical DBD** to physical segments, then use the **physical DBD**
for hierarchy, cardinality, and key uniqueness.

| Input | Location | What it tells you |
|-------|----------|-------------------|
| **Source IMS call** | `<repo-root>/legacy/COB/<PGM>.COB` | Function code (GU/GNP/…), target area, SSA list & operators (`=`, `>`) |
| **Logical DBD (MANDATORY)** | `<repo-root>/db/ims/DBD/<db>L*.DBD` (e.g. `JAHAL00P.DBD`) | What each SSA name really is: `SOURCE=` maps a logical segment to physical segment(s)/DBD; logical relationships (`KEY`) |
| **Physical DBD (MANDATORY)** | `<repo-root>/db/ims/DBD/<db>F*.DBD` (e.g. `JAHAF00P.DBD`) | Hierarchy (`PARENT=`), cardinality (`FREQ=`), key uniqueness (`SEQ,U`), key length (`BYTES=`) |
| **DBDICT.CSV** | `<repo-root>/db/ims/DBD/DBDICT.CSV` | Per-field: physical segment → `Table` and `Column`, offsets, lengths, key flags |
| **.record files** | `<repo-root>/db/ims/DBD/<Segment>.record` | The record's `table=`, its `keyNumber` columns, and `<parent>`/`<parentKeys>` |
| **DBD.CSV** | `<repo-root>/conversion/DBD.CSV` | Segment key fields: `DBID,Segment,KeyField,Offset,Len,Type,KeyFlag` |

### Reading the logical DBD (do this FIRST)

The program's PCB sees the **logical** database. SSA names like `JPOSS101` are
*logical* segment names that do **not** exist as physical tables. The logical DBD
(`ACCESS=LOGICAL`, `DATASET LOGICAL`) maps them with `SOURCE=`:

```
 SEGM  NAME=JPOSS101,PARENT=JAHAS004,
       SOURCE=((JAHAS010,,JAHAF00P),(JPOSS001,KEY,JPOSF00P))
 SEGM  NAME=JPOSS021,PARENT=JPOSS101,
       SOURCE=((JPOSS021,,JPOSF00P))
```

How to read `SOURCE=`:
- **One part** — `SOURCE=((JPOSS021,,JPOSF00P))` — the logical segment IS the
  physical segment `JPOSS021` in DBD `JPOSF00P`. Straight passthrough.
- **Two parts** — `SOURCE=((JAHAS010,,JAHAF00P),(JPOSS001,KEY,JPOSF00P))` — this
  is a **logical relationship**. The first is the *physical data segment*
  (`JAHAS010`, whose concatenated key is the row's key); the second, tagged
  `KEY`, is the *destination/paired* segment (`JPOSS001`) whose key is
  concatenated in. So logical `JPOSS101` = physical `JAHAS010` data +
  `JPOSS001` key path. This is why `SSA-JPOSS101-ORDPOSNR` uses `ORDPOSNR`
  (that is `JAHAS010`'s 14-byte key).

Use the logical DBD to translate **every** SSA segment name to its physical
segment before touching DBDICT/`.record`.

### Reading the physical DBD

Each segment is a `SEGM` line; its key is the next `FIELD` line
(`JAHAF00P.DBD`):
```
 SEGM   NAME=JAHAS004,BYTES=3,FREQ=2,PARENT=JAHAS003,PTR=TB
 FIELD  NAME=(BPRUTIN,SEQ,U),BYTES=3,START=1,TYPE=C
 SEGM   NAME=JAHAS010,BYTES=14,FREQ=10,PARENT=((JAHAS004,SNGL),(JPOSS001,P,JPOSF00P))
 FIELD  NAME=(ORDPOSNR,SEQ,U),BYTES=14,START=1,TYPE=C
 SEGM   NAME=JAHAS011,BYTES=350,FREQ=1,PARENT=JAHAS010,PTR=NT
```
Extract:
- **`PARENT=`** — hierarchy path (which SSAs are navigation above the target).
- **`FREQ=`** — children per parent. `FREQ=1` under a unique-keyed ancestor ⇒
  exactly one row ⇒ the leaf key alone is sufficient; drop parent SSAs.
- **`FIELD NAME=(key,SEQ,U)`** — `SEQ,U` = unique sequence key. If a unique
  ancestor key is denormalized into the target table, that key alone locates the
  row; higher parent SSAs are pure navigation and drop out.
- **`BYTES=`/`START=`** — field length/offset, confirming key decomposition
  (e.g. `ORDPOSNR BYTES=14` splits into 10+4 per the `.record`).

DBDICT.CSV header columns (the two that matter are `Table` col 20, `Column` col 21):
```
DBID,TblCode,File,Copybook,Field,Level,Type,Offset,Len,Pic,Occur,Digits,Dec,
Discrete,Redef,Issue,Soltn,Date Type,Map,Table,Column,SQL Type,SpFmt,Nulls,
Default,Scan,Cond,Seq
```

## Procedure

1. **Identify the target segment.** The 3rd `USING` argument's segment, e.g.
   `JPOSS021-AREA` → logical segment `JPOSS021`.

2. **Resolve every SSA name via the LOGICAL DBD.** Translate each SSA's logical
   segment to its physical segment(s) using `SOURCE=`. Note any two-part
   (logical-relationship) sources — the first part is the data/key segment.

3. **Read the PHYSICAL DBD for the target's physical segment.** Record its
   `PARENT=` chain, `FREQ=`, and which ancestor keys are `SEQ,U`. This decides
   how many parent SSAs are pure navigation (droppable) and whether the read
   returns one row or many.

4. **Find the target's SQL table.** Open `<Segment>.record` and read `table="…"`
   (or grep DBDICT.CSV `<DBID>,<PhysSegment>,` and read the `Table` column). This
   is the ONLY table in `FROM`.

5. **Find that table's key columns.** In the `.record`, read `keyNumber="1"`,
   `keyNumber="2"`… and their `column="…"`. Also read `<parentKeys>` — parent key
   columns **denormalized into this table** (they ARE columns here).

6. **Map each qualifying SSA to a column — only if the column exists in the
   target table.**
   - Column exists (per DBDICT for the target table) → it becomes a `WHERE`
     predicate.
   - Column lives only in an ancestor table → **drop it**. The `PARENT=` chain
     plus a unique (`SEQ,U`) ancestor key already guaranteed the path; you cannot
     and need not filter on a non-existent column.

7. **Handle concatenated keys.** One IMS key can span multiple SQL columns (e.g.
   `ORDPOSNR` X(14) = `CustomerOrderNumber`(10) + `POSNR`(4)). If the `.record`
   stores it as one column, use it whole; if split into `keyNumber` sub-columns,
   decompose with `Subfield`.

8. **Translate the operator:**
   - `=` (EQ) on a composite key → `col1 = ? AND col2 = ?` (or single `=` if whole).
   - `>` (GT) on a composite key → lexicographic
     `(col1 > ? OR (col1 = ? AND col2 > ?))`; on a single VARCHAR column, `col > ?`.
   - Add `ORDER BY <keys>` for sequential reads (GN/GNP/GHNP) and GT-positioned
     reads, using the verified hierarchy/key column order.

9. **Choose the fetch method** (keep consistent with converter intent):
   - `GU` unqualified-unique / "read one" → `Select(…)` then `Fetch()`.
   - `GU`/`GN` positioned scan (GT) → `Select(… ORDER BY …)` then `Fetch()`.
   - Truly single expected row → `FetchOne()` (fails with status if >1 row).

10. **Count parameters.** They must exactly match the `?` placeholders, in order.
    A common bug is leftover parameters from dropped SSAs.

## ExperLib SELECT API

`SqlRecord.Select` is the workhorse:
```csharp
bool Select(string sql, params object[] args);
bool Select(SqlSelectOptions? options, string? sql, params object[] args);
```
`BuildSql` behavior you can rely on:
- If `sql` starts with `SELECT ` → used verbatim.
- Otherwise ExperLib prepends `SELECT * FROM <mainSchemaTable> ` and, unless the
  fragment starts with `ORDER BY`, inserts `WHERE `. Predicate-only input is
  supported by the API, but must not be emitted by this skill.
- `SqlRecord.READONLY` / `TOP1` / `READONLY_TOP1` add `WITH (NOLOCK)` / `TOP n`.

### Required C# SELECT output format

- Always emit a complete SQL statement inside a multiline C# verbatim string
  starting with `@"SELECT *`, including `FROM <verified target table>` and any
  required `WHERE` predicates. Do not emit predicate-only `Select()` calls.
- Preserve the existing C# record variable (for example, `jckvs068`); do not
  rename it after the SQL table.
- Put SQL clauses on separate lines, with one `WHERE` predicate per line.
- Include `ORDER BY` for sequential reads (GN/GNP/GHNP) and GT-positioned reads,
  using the verified hierarchy/key column order, including composite key parts.
- Preserve existing locking behavior. Include `WITH (NOLOCK)` after `FROM` when
  required by the program's established read convention; do not introduce dirty
  reads merely to change formatting.
- Preserve existing select options and row limits when expanding short-form SQL.
- Put each parameter on a separate line, in placeholder order, after the SQL
  string. Keep the existing `Fetch()`/`FetchOne()` behavior.

Example (locking hints are omitted here; preserve them when required by the
program):
```csharp
jposs021Area.Select(
    @"SELECT *
        FROM CustomerOrderLine
       WHERE CustomerOrderNumber = ? AND
             POSNR = ?
       ORDER BY CustomerOrderNumber, POSNR",
    Subfield(ssaOrdposnr, 0, 10),
    Subfield(ssaOrdposnr, 10, 4));
jposs021Area.Fetch();
```

Other read methods: `SelectChildren(parent, parentKeys[, childKeys], sortKeys, args)`
(emits `WHERE childKey = ?` from parent column values), `ReadByKey(params)`,
`ReadOne(sql, args)` (status 02 if >1), `SelectAll()`.

### `Subfield` helper
```csharp
Subfield(object o, int start)            // substring start→end, right-trimmed
Subfield(object o, int start, int length)// substring [start, start+length), right-trimmed
```
Zero-based offsets. Use to split concatenated IMS keys into SQL sub-columns.

## Worked Example (J5050023, paragraph A080)

```cobol
CALL 'CBLTDLI' USING GNP DB1DBPCB JPOSS021-AREA
     SSA-JAHAS001-JAHAKOD-EQ    (JAHAKOD  = bearbetningstyp)
     SSA-JAHAS003-ORDERNR-EQ    (ORDERNR  = ordernr)
     SSA-JAHAS004-BPRUTIN-EQ    (BPRUTIN  = beppeRutin)
     SSA-JPOSS101-ORDPOSNR-EQ   (ORDPOSNR = ordposnr)
     SSA-JPOSS021-OKVAL.        (unqualified target)
```

Tracing:
- Logical DBD: `JPOSS021 SOURCE=((JPOSS021,,JPOSF00P))` → physical `JPOSS021`.
  `JPOSS101 SOURCE=((JAHAS010,,JAHAF00P),(JPOSS001,KEY,JPOSF00P))` → logical
  segment; its key `ORDPOSNR` is `JAHAS010`'s key pointing to `JPOSS001`.
- Physical DBD: `JPOSS021` `FREQ=1, PTR=NT` under root `JPOSS001` whose key
  `ORDPOSNR` is `SEQ,U` (unique) → exactly one `JPOSS021` per `ORDPOSNR`.
- `Jposs021`/DBDICT → table **`CustomerOrderLine`**.
- `Jposs001.record` keys: `ORDPOSNR` = `CustomerOrderNumber`(10, keyNumber 1) +
  `POSNR`(4, keyNumber 2).
- Parent SSAs `JAHAKOD` / `BPRUTIN` live in the JAHA database, **not** in
  `CustomerOrderLine` → drop them (pure path navigation; `ORDPOSNR` alone is
  unique).

Correct query (2 parameters):
```csharp
jposs021Area.Select(
    @"SELECT *
        FROM CustomerOrderLine
       WHERE CustomerOrderNumber = ? AND
             POSNR = ?
       ORDER BY CustomerOrderNumber, POSNR",
    Subfield(ssaOrdposnr, 0, 10),
    Subfield(ssaOrdposnr, 10, 4));
jposs021Area.Fetch();
```

Converter's wrong output (for contrast):
```csharp
// WRONG: two unrelated tables, no JOIN (cartesian), filters on columns that
// don't exist in CustomerOrderLine, 6 params instead of 2, SELECT A.*
"SELECT A.* FROM JAHAS004 A, CustomerOrderLine B WITH (NOLOCK)
  WHERE A.BEARBETNINGSTYP = ? AND A.CustomerOrderNumber = ? AND
        A.BEPPE_RUTIN = ? AND B.CustomerOrderLineNumber = ? AND
        A.CustomerOrderNumber = ? AND B.POSNR = ?"
```

## Worked Example (J5051613, paragraph A020 — whole-key variant)

Same SSA path as A080 above, but the target is `JAHAS011` (not `JPOSS021`), and
the key stays **whole** — a good contrast for when NOT to `Subfield`.

```cobol
CALL 'CBLTDLI' USING GNP DB1DBPCB JAHAS011-AREA
     SSA-JAHAS001-JAHAKOD-EQ    (JAHAKOD  = bearbetningstyp)
     SSA-JAHAS003-ORDERNR-EQ    (ORDERNR  = ordernr)
     SSA-JAHAS004-BPRUTIN-EQ    (BPRUTIN  = beppeRutin)
     SSA-JPOSS101-ORDPOSNR-EQ   (ORDPOSNR = hh-ordposnr, set in A010)
     SSA-JAHAS011-OKVAL.        (unqualified target)
```

Tracing:
- **Target** = 3rd arg `JAHAS011-AREA` → logical segment `JAHAS011`.
- **Logical DBD** `JAHAL00P.DBD`: `JAHAS011 SOURCE=((JAHAS011,,JAHAF00P))` —
  one-part passthrough → physical `JAHAS011`.
- **Physical DBD** `JAHAF00P.DBD`: `JAHAS011 FREQ=1, PARENT=JAHAS010`; parent
  `JAHAS010`'s key `ORDPOSNR` is `SEQ,U` (unique, 14 bytes) ⇒ exactly one
  `JAHAS011` per `ORDPOSNR`.
- **Table** from `Jahas011.record`: `table="JAHAS011"`.
- **Key column**: DBDICT maps `JAHAS010 ORDPOSNR X(14)` →
  `CustomerOrderLineNumber VARCHAR(14)`, flagged `KEYU`, stored **whole** (the
  `.record` keeps it as one `CustomerOrderLineNumber` column, NOT split into
  10+4). The `<parentKeys>` block denormalizes all four ancestor keys, but the
  unique `CustomerOrderLineNumber` alone locates the row.
- Parent SSAs `JAHAKOD` / `ORDERNR` / `BPRUTIN` are pure path navigation →
  **drop them** (redundant given the unique leaf key).
- **Parameter provenance**: `SSA-ORDPOSNR` holds `HH-ORDPOSNR`, set at line 021500
  *after* the A010 `GU` — NOT the `WS-KSNR-KSPOS` used to qualify A010, and
  nothing in A020 re-moves it. Always trace the last `MOVE ... TO SSA-ORDPOSNR`
  before the call.

Correct query (1 parameter, whole key, no `Subfield`):
```csharp
jahas011Area.Select(
    @"SELECT *
        FROM JAHAS011
       WHERE CustomerOrderLineNumber = ?
       ORDER BY CustomerOrderLineNumber",
    ssaOrdposnr);
jahas011Area.Fetch();
```

Contrast with A080: A080's `JPOSS021` lives in `CustomerOrderLine` where the key
is split into `CustomerOrderNumber`(10)+`POSNR`(4), so it needs two `Subfield`
params. `JAHAS011` stores the key as one `VARCHAR(14)` column, so it takes the
value whole — one param, no split. **The `.record` column layout decides whether
to split, not the COBOL.**

## Paired Logical Relationships (both `SOURCE=` parts untagged)

Some logical DBD `SOURCE=` entries have **both** parts untagged (no `KEY`/`DATA`
tag), e.g.:
```
JCKVS201 SOURCE=((JPOSS010,,JPOSF00P),(JCKVS001,,JCKVF00P))
```
This is a **mutual/paired** logical relationship, not the usual key-vs-data
two-part source. Resolve it by finding the `LCHILD ... PAIR=` directive in
**each** physical DBD:
```
# JCKVF00P.DBD
LCHILD NAME=(JPOSS010,JPOSF00P),PTR=NONE,PAIR=JCKVS080
# JPOSF00P.DBD
LCHILD NAME=(JCKVS080,JCKVF00P),PTR=NONE,PAIR=JPOSS010
```
This confirms the logical segment's DATA comes from the **paired physical
segment on the other side** — here, `JCKVS201` reads as physical `JCKVS001` →
table `QualityControlBatch`. It is NOT a join of both segments; pick the one
that is the actual data-bearing side via the `PAIR=` cross-reference.

## Shared `dbIoarea`-style Object Across Sequential Queries (per-program C# idiom)

Some converted `.cs` files (e.g. `J9010113.cs`) use **one shared record object**
(commonly named `dbIoarea`), repointed at a different SQL table for every
`Select()`/`Fetch()` call, inside one big flat method with `Label:`/`goto`
mirroring the COBOL paragraphs and `PERFORM`/`GO TO` (not separate C# methods
per paragraph). Because the shared object is overwritten by each subsequent
`Select()`, any join-key value needed by a **later** query further down the
hierarchy must be captured into a local C# variable **before** the next
`Select()` overwrites it:
```csharp
var ekval = dbIoarea.BposplatEkval;   // capture BEFORE next Select() overwrites dbIoarea
var refnr = dbIoarea.BposplatRefnr;
```
Then issue the next `dbIoarea.Select()` with a complete multiline SQL statement
against the verified target table, passing `ekval` and `refnr` on separate lines,
and call `dbIoarea.Fetch()`.
Since it is all one flat method scope, declaring these locals earlier in the
sequential flow keeps them in scope for later labels reached via fall-through or
backward `goto`. Verify safety with a grep for `goto <label>;` across the file to
confirm no external jump reaches a label that uses the variable without first
passing through its declaration.

## FREQ=1 vs FREQ>1: Collapsing COBOL GN Retry-Loops

COBOL often wraps a single `CBLTDLI GN` call in a "loop back on mismatch"
pattern (`PERFORM ... GO TO A0xx`, or in already-converted C# a
`while (true) { ...; continue; }`), relying on IMS to fail the 2nd call once the
(single) row is exhausted. Keeping this loop as-is when converting to SQL is a
bug:
- If the physical child segment has **`FREQ=1`** (at most one row per parent),
  **collapse the loop**: a single `Select()` + `Fetch()`, then branch directly
  with no `continue`/retry. A 2nd `Select()`+`Fetch()` with identical parameters
  would just re-return the same row forever (infinite loop) — unlike real IMS
  `GN`, which naturally fails once the single row is exhausted.
- If **`FREQ>1`** (genuine multi-row, e.g. `FREQ=5`), keep the loop but call
  `Select()` **once, outside/before** the loop (with `ORDER BY` the segment's
  sort key), then call `Fetch()` **repeatedly inside** the loop to advance
  through rows — `Fetch()` is sequential and advances the same result set.

## Common Converter Bugs to Look For

| Symptom | Why it's wrong | Fix |
|---------|----------------|-----|
| Logical SSA name treated as a table | It's a logical segment; resolve `SOURCE=` first | Use the physical segment's table |
| Two tables in `FROM` with a comma | Parent SSA treated as a table, not path nav | Single table = target segment's `table` |
| No JOIN between the two tables | Cartesian product | Remove the extra table entirely |
| `SELECT A.*` where A is the parent | Returns parent columns, not the target segment | `SELECT * FROM <targetTable>` |
| Filters on `BEARBETNINGSTYP`, `BEPPE_RUTIN`, etc. | Those columns live in an ancestor table | Drop predicates whose column isn't in the target table |
| GT expanded across a whole-key column *and* its sub-columns | Mixes VARCHAR(14) key with its (10)+(4) split | Pick one representation, not both |
| Extra `?` parameters | Leftovers from dropped SSAs | Parameter count must equal `?` count |
| Missing `ORDER BY` on GN/GNP/GT | Sequential retrieval needs key order | Add `ORDER BY <keyColumns>` |

## Verification Checklist

- [ ] Pre-flight passed: program `.COB`, physical DBD, and logical DBD all found in the workspace (else asked the user to upload the missing file).
- [ ] Logical DBD read: every SSA name resolved via `SOURCE=` to a physical segment.
- [ ] Physical DBD read: `PARENT=` chain, `FREQ=`, and `SEQ,U` key flags confirmed.
- [ ] `FROM` names exactly one table = target segment's `.record` `table=`.
- [ ] `Select()` uses a complete multiline C# verbatim SQL string, not a predicate-only fragment.
- [ ] Existing C# record variable, select options, row limits, and locking behavior are preserved.
- [ ] SQL clauses and predicates are on separate lines; each parameter is on its own line.
- [ ] Every WHERE column exists in that table (confirmed via DBDICT `Column`).
- [ ] Composite keys split consistently (all whole, or all `Subfield`).
- [ ] Operator matches SSA (`=` → equality; `>` → lexicographic OR-chain / simple `>`).
- [ ] `ORDER BY` present for sequential (GN/GNP/GHNP) or GT-positioned reads, in verified hierarchy/key column order.
- [ ] `?` count == parameter count, in order.
- [ ] Fetch method matches intent (`Fetch` vs `FetchOne`).
- [ ] If `SOURCE=` has two untagged parts (paired logical relationship), cross-referenced `LCHILD ... PAIR=` in both physical DBDs to find the data-bearing side.
- [ ] If the file uses a shared `dbIoarea`-style object across calls, join-key values needed later are captured into local variables before being overwritten.
- [ ] `FREQ=1` retry-loops collapsed to a single `Select()`+`Fetch()`; only genuine `FREQ>1` segments keep a loop (`Select()` once, `Fetch()` repeatedly inside).
