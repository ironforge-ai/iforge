# iforge brand mark candidates

Three concepts for the mark that replaces the artwork the previous phase renamed
but never redrew. Exactly one ships. Everything downstream is blocked on the
choice: six generated asset files, the manifest theme colour, the newly wired
SVG favicon, and three in-app logo sites, one of which is a public
unauthenticated share page.

## Decision — **A, the monogram, ships**

Selected by the user at the `checkpoint:decision` gate in `03.3-12-PLAN.md`
(`gate="blocking-human"`), resolved 2026-09-10 with `Select: monogram`. This is
a human decision made against the contact sheet, not an auto-approval.

**The mark is `monogram.svg`** — the lowercase `i` standing on a base bar,
knocked out of a solid rounded plate. `viewBox 0 0 512 512`, single `fill` on the
root: `#c96442` light, `#d97757` dark.

**Why it won:** it is the only candidate that survives the binding 16px
constraint *while still reading as a mark*. At 16 pixels it carries 51% ink
coverage across the frame, the stem holds at 2.6px, and the dot and the base bar
both survive as separate marks. The other two remain *shapes* at that size but
stop being identifiable as this product. Distinctiveness was the trade made, and
it was made knowingly — see the recommendation at the bottom of this file, which
the decision accepted.

**Downstream:** plan 03.3-13 promotes this file to `brand/iforge-mark.svg` and
generates the entire shipped asset set from it. Because A was chosen and not C,
03.3-13 stands exactly as written — the three in-app logo call sites keep their
square boxes and are not edited.

### The two that were considered and rejected

They stay in this directory and stay documented below. They are the reasoning
behind the choice, not leftovers; a candidates directory that erases the
alternatives loses the argument that produced the winner.

| Rejected | Recorded weakness that decided it |
|---|---|
| **B — `geometric.svg`**, the two-mass anvil | At 16px it becomes an asymmetric mass that no longer says "anvil" — the horn survives only as a spur and the form stops being nameable. It also clips **destructively** under a circular mask: the 3.5% of ink outside the 409 circle is the horn tip and the far corner of the face, which is precisely the 3.5% that makes the shape an anvil rather than a lump. It was already redrawn once during Task 1 (waisted body → solid tapering body) because the first version read as a serif capital `I` at 16px and collided with A. |
| **C — `wordmark.svg` + `wordmark-monogram.svg`**, the pairing | Two artworks to keep in step instead of one, and the companion `F` alone in a browser tab does not obviously belong to a product called iforge — the wordmark that supplies that context lives in the sidebar, not the tab. C also carries a real structural cost: the three in-app logo sites render in **square** boxes today, so a 2.66:1 lockup would need all three widened and plan 03.3-13 amended before it could run (see the last section of this file). Choosing A avoids that entirely. |

---

## How to look at them

```
open brand/candidates/contact-sheet.html
```

Any browser, no server, no build step. The sheet renders every candidate at 16,
32, 180 and 512 pixels, on the light ground and the dark ground, in the accent
and in a single neutral, and draws the 409px maskable safe circle over a 512px
render.

**Judge at 16px first.** It is the binding constraint (P33-17), not a
preference. Every other size forgives more than it does.

## What was drawn against what

| # | File | Concept | Drawn against | Weakest size |
|---|---|---|---|---|
| A | `monogram.svg` | Lowercase `i` standing on a base bar, knocked out of a solid rounded plate | 16px legibility above all else | none — it is the only candidate that does not degrade at 16px |
| B | `geometric.svg` | An anvil cut down to two solid masses: a face tapering left into a horn, a body flaring to a foot | Distinctiveness and a symbol that carries no letterform | **16px** — the horn survives as a spur but the form stops being nameable |
| C1 | `wordmark.svg` | The full name in a squared geometric alphabet drawn as outlines | Legibility of the *name* in the in-app logo box | **anything square** — it is a 2.66:1 horizontal lockup |
| C2 | `wordmark-monogram.svg` | A squared `F` for forge, same construction as the wordmark | Carrying the square icon sizes that C1 cannot | 16px, mildly — the middle arm thins but the `F` still reads |

C1 and C2 are one candidate, not two. The wordmark takes the in-app logo, the
companion takes every square icon size, and the two must be regenerated
together or the pair drifts apart.

## Why the companion is an `F` and not an `i`

The wordmark already carries the `i`. A lone lowercase `i` at 16px is a bar and
a dot, which says nothing and would be indistinguishable from a loading bar. The
icon half therefore takes the *forge* initial. Moot now that A ships, but kept
because it is why C1 and C2 are one candidate rather than two — and because if
the pairing is ever revisited, the `F` is the reason it needs a companion at
all.

## Constraints every candidate is built to

- **Single-colour flat geometry.** No gradient, no filter, no embedded raster,
  no external font reference. This is a *build* constraint, not taste:
  `rsvg-convert` is absent on this host, so ImageMagick falls back to its
  internal MSVG renderer, which handles flat filled paths correctly and little
  else.
- **Colour in exactly one place.** Each file carries a single `fill` on its root
  element: `#c96442`, the sRGB of the light-mode `--primary` token. The dark
  accent is `#d97757`. One file, one attribute, both themes.
- **No `currentColor`.** It was the first thing tried and it renders as *nothing
  at all* under MSVG — an empty canvas, silently. Any downstream generator that
  switches these files to `currentColor` will produce blank PNGs and the failure
  will not announce itself. See the comment at the top of each SVG.
- **Letterforms are outlines, not text.** No `<text>` element and no font
  reference anywhere, so nothing depends on a font being installed.
- **Nothing traced.** Every path was authored from scratch against the iforge
  name and the forge metaphor. The existing `apps/web/public/iforge.svg` is a
  machine trace of the raster being replaced; it was never opened, never
  imported, and is not an ancestor of anything here (MIGR-04).

## The maskable safe zone — measured for all three, and for the chosen mark

The safe zone is a centred 409×409 region of the 512×512 canvas, about 80%.
**All three candidates sit inside that square.** Measured from the 512px raster:

| Candidate | Ink bounds | Inside the 409 square | Inside the 409 *circle* | Ink outside the circle |
|---|---|---|---|---|
| `monogram.svg` | 52–460 × 52–460 | yes | no | 21.5% |
| `geometric.svg` | 52–452 × 116–396 | yes | no | 3.5% |
| `wordmark-monogram.svg` | 131–381 × 52–460 | yes | no | 7.8% |

No candidate fits inside the 409px *circle*, because a mark that fills a 409
square necessarily has corners outside the circle inscribed in it. This is not
currently a defect: plan 03.3-13 **drops the maskable manifest entry entirely**
(P33-19), so nothing ships declaring padding it does not have. Shrinking the
marks to be circle-safe would cost about 29% of their size and would damage the
one constraint that is binding — 16px legibility — to satisfy a declaration that
is being removed.

What matters if a padded maskable asset is ever added later (the sanctioned
"seventh path", which needs MIGR-04's count re-amended first):

- **A** clips benignly. The plate's corners fall outside the circle but the
  entire `i` knockout sits within radius 182 of 204, so a circular mask turns
  the plate into a circular badge with the letter fully intact.
- **B** clips *destructively*. The part that falls outside is the horn tip and
  the far corner of the face — 3.5% of the ink by area, but it is the 3.5% that
  makes the shape an anvil rather than a lump.
- **C2** clips at both ends of the `F`: the top arm's right end and the stem's
  foot.

## Recommendation — accepted

**A, the monogram.** Not because it is the most interesting — it is the least
interesting of the three, and a letterform in a rounded plate is the most common
shape in this category — but because it is the only one that answers the
constraint that actually decides this.

At 16px the monogram is a filled plate with a letter cut into it: 51% of the
frame carries ink, the stem is 2.6px wide, the dot and the base bar both survive
as separate marks, and the whole thing still reads as `i`. B at the same size is
an asymmetric mass that no longer says "anvil". C2 reads as an `F`, cleanly, but
an `F` alone in a browser tab does not obviously belong to a product called
iforge — the wordmark that supplies the context is in the sidebar, not the tab.

The monogram is also the only candidate whose mass comes from the plate rather
than from the strokes, which is why it is the one that degrades gracefully:
there is no thin feature for the rasteriser to lose.

**This was written as a recommendation, not the decision.** The choice was made
at the `checkpoint:decision` gate in `03.3-12-PLAN.md`, marked
`gate="blocking-human"` and never auto-approved. The user selected **A, the
monogram**, on 2026-09-10, accepting this reasoning. See the Decision section at
the top of this file.

## Known cost of choosing C, the wordmark pairing — not incurred

**C was not chosen, so none of the following applies to what ships.** It is kept
because it is part of why A won.


The three in-app logo sites render the logo in **square** boxes today —
`size-9` (36px) at `app-sidebar.tsx:1052`, `size-10` (40px) at
`redeem-page.tsx:39`, and an explicit `width={28} height={28}` at
`public-share/session-page.tsx:264`. A 2.66:1 wordmark dropped into a 36px
square box has an x-height of roughly 6px. The contact sheet shows exactly this,
in the last panel of the C1 section.

Choosing C therefore means those three call sites need wider boxes — and plan
03.3-13 is written to leave them untouched, with a verification gate that
*fails* if any of the three files is edited. Picking C means 03.3-13 has to be
amended before it runs. Picking A or B leaves it as written.
