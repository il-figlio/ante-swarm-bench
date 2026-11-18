#!/usr/bin/env python3
"""Generate the notes/ knowledge-base corpus and tests/manifest.json.

Deterministic (fixed seed): re-running rewrites notes/ and
tests/manifest.json byte-identically.

    python3 tools/gen_kb.py

Corpus: one person's markdown knowledge base — project plans, standing
areas, reference notes, and dated daily entries — kept in local files on
purpose. Cross-links are relative markdown links (see README). Ten links
are deliberately broken (renamed or moved targets, typos); the manifest
records exactly which, and a sha256 of every note with link targets
stripped, so a fix task can be checked without touching prose.
"""
import hashlib
import json
import random
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = 20260817

# ---------------------------------------------------------------- notes
# Authored content. Keys are paths under notes/. The broken links are
# written in directly; FIXES below records the intended target of each so
# the manifest can carry ground truth.

NOTES = {}

# ---- projects ---------------------------------------------------------

NOTES["projects/ceramics-studio.md"] = """\
# Ceramics studio

Slow-burn side project: hand-thrown stoneware, sold at two craft
markets a year and through the [studio website](studio-website.md).

## Where things stand

- Wheel: Shimpo Whisper, in the [garage workshop](garage-workshop.md).
- Kiln: shared firing at Clayworks co-op, ~EUR 38 per shelf per firing.
- Current body: St. Thomas stoneware, grog-free for mugs.
- Glaze tests live in [the glaze notebook](../resources/glaze-notebook.md);
  keep new combos there, not in this file.

## Next drops

- [ ] 12 mugs + 4 bowls for the October market
- [ ] reshoot product photos — daylight, north window only
- [ ] price check against last year's sheet (see
      [budget](../projects/budget-2027.md) for the materials line)

## Ideas parking lot

- Spoon rests — everyone asks, nobody has made them locally #idea #ceramics
- Seconds bin at cost for newsletter subscribers
- Collab tile run with Ana's letterpress (see
  [zine issue 3](zine-issue-3.md) for the paper contact)
"""

NOTES["projects/studio-website.md"] = """\
# Studio website

Static site, hand-rolled HTML+CSS, hosted on a nearly-free VPS but
editable fully offline. Deliberately no analytics, no fonts, no JS.

## Structure

- `index.html` — gallery grid + a one-line bio
- `shop.html` — "email me" links per piece, no cart
- `notes.html` — occasional process posts, drafted as markdown here first

## To do

- [ ] rebuild the gallery page from the October firing
- [ ] add an RSS file by hand (it's one XML file, stop putting it off)
- [ ] compress photos to <200 KB; keep originals in
      [the photo archive](family-photo-archive.md) flow

## Notes

Preview locally with `python3 -m http.server` — see
[cli cheat sheet](../resources/cli-cheat-sheet.md). Keep it dependency-free;
if it needs a build step it's already too complex. #web #local
"""

NOTES["projects/garage-workshop.md"] = """\
# Garage workshop

Converting the garage's back third into a usable bench + wheel space.

## Constraints

- Power: one 16A circuit; kiln is out, wheel + lights are fine
- Winter: uninsulated roller door — foam panels before December
- Dust: clay + grinder don't mix; wheel goes in the far corner with a
  washable curtain

## Purchases

- [x] workbench top (reclaimed bowling-lane maple, 180x70)
- [ ] LED battens x4
- [ ] floor paint, epoxy, two coats
- [ ] French cleat rail for hand tools

## Links

Feeds the [shed roof repair](shed-roof-repair.md) material list —
same timber merchant. Household maintenance queue lives in
[home maintenance](../areas/home-maintenance.md). #house
"""

NOTES["projects/language-exam.md"] = """\
# DELE B1 — May sitting

Target: DELE B1 in May, mostly for the move research in
[move to Valencia](move-to-valencia.md). Daily practice lives in
[the Spanish area note](../areas/spanish.md).

## Score plan

- Reading/listening: past papers, one per week from February
- Writing: two timed letters a week, corrected by intercambio partner
- Speaking: the weak one — book a tutor for April, 6 sessions minimum

## Resources

- Cramér "Gramática de uso" B1 — working through it linearly
- [podcasts](../resources/podcasts.md) — the Spanish section only
- Past papers PDF folder on the NAS (offline copies, obviously)

## Log

- 2026-09-04: mock exam, reading 21/25, writing marked "just over"
- 2026-10-08: subjunctive finally clicking after the song-lyrics drill
#spanish
"""

NOTES["projects/sourdough-pop-up.md"] = """\
# Sourdough pop-up

One-Saturday bakery stall with Marta — her kitchen, my oven + racks.
Sixty loaves cap; anything more needs a commercial kitchen licence
we're not getting for a hobby.

## Menu

- Country loaf (the [sourdough recipe](../resources/recipes-sourdough.md),
  doubled, 78% hydration)
- Seeded rye, 60/40
- Focaccia slabs, rosemary + flaky salt

## Costs (draft)

- flour ~25 kg organic: EUR 55
- bags + labels: EUR 30
- stall fee: EUR 45

Split costs and revenue 50/50; track in
[finances](../areas/finances.md) under a "popup" tag.

## Open questions

- [ ] pre-orders via paper sign-up at her café, or honour-system list?
- [ ] pricing: EUR 7 country / 8.50 rye feels right
#food
"""

NOTES["projects/family-photo-archive.md"] = """\
# Family photo archive

Scanning the five shoeboxes of prints + two albums before they fade
further. Everything lands on the NAS and into the
[backup routine](../areas/backup-routine.md).

## Process

- Flatbed at 600 dpi TIFF for keepers, 300 dpi JPEG for the rest
- Filename: `YYYY-MM[-DD]-descr-NNN.ext` when a date is known,
  `undated/…` when not
- Faces get a text file per box listing who's who — aunt Rosa is the
  oracle here, book a weekend with her before winter

## Progress

- box 1 (1978–86): done, 214 scans
- box 2 (1987–93): ~half
- albums: not started; spines are glued, need the spacers

## Film note

My own negatives go through [film photography](../resources/film-photography.md)
workflow instead — different scanner, different folder. #photo
"""

NOTES["projects/bike-tour-alps.md"] = """\
# Bike tour: the Alps, June

Eight days, Bregenz → Bolzano, mostly paved passes. Gear list is in
[the bike tour checklist](../resources/bike-tour-checklist.md) — it
moved here from the general pile, link it correctly if it looks odd.

## Route draft

- day 1: Bregenz → Feldkirch, flat warm-up
- day 3: Flexenpass — the one I'm training for
- day 5: Reschenpass, then it's mostly downhill-adjacent
- day 8: Bolzano, train back

## Training

- base miles from [strength training](../areas/strength-training.md)
  cross-training days
- two loaded weekend rides in April, 120 km each

## Budget line

~EUR 90/day half-board guesthouses — folded into
[the 2027 budget](budget-2027.md). #bike
"""

NOTES["projects/bike-tour-checklist.md"] = """\
# Bike tour checklist

Gear that has to be on the bike or on me. Lives with the tour plan in
[bike tour alps](bike-tour-alps.md); overlaps the generic
[packing list](../resources/packing-list.md) but adds spares.

## On the bike

- 2 spare tubes + patches + tyre levers
- mini pump, multitool, chain link x2
- front/rear lights even in June (tunnels)
- frame bag: phone, cash, passport copy, cable lock

## On me / pannier

- kit x2, one warm layer, rain shell
- sandals for evenings — non-negotiable luxury
- electrolyte tabs, 2x bottles, water filter (some fountains are marked
  non-potable)

## Paperwork

- EHIC card photocopy
- guesthouse confirmations printed — phone-only is asking for it
#bike
"""

NOTES["projects/budget-2027.md"] = """\
# Budget 2027

Yearly reshuffle of categories; the standing rules are in
[finances](../areas/finances.md). This note is only the 2027 numbers.

## Changes vs 2026

- groceries +12% — honest number this time, not aspirational
- travel: Alps tour + two Valencia scouting trips
  ([move to Valencia](move-to-valencia.md))
- "studio" is now its own category instead of hiding in hobby

## Fixed

- rent 1150 · utils ~210 · NAS/storage ~9 · transit pass 58
- insurance bundle 142 — requote in March

## Targets

- savings rate ≥ 28%
- emergency fund to 6 months by October (currently 4.2)
- ceramics revenue is upside, not budgeted

## Ritual

First Sunday of the month: export bank CSV, reconcile in the
spreadsheet, update the burn chart. No apps, no sync. #finance
"""

NOTES["projects/shed-roof-repair.md"] = """\
# Shed roof repair

Felt is cracked along the ridge and one sheet is lifting — patch now,
full re-felt in spring.

## Materials

- 3 m roll of torch-on felt offcut (timber merchant, see
  [garage workshop](garage-workshop.md) supplier note)
- clout nails 20 mm, galvanised
- bitumen paint, 2.5 L
- replacement ridge board section: 1.2 m pressure-treated

## Sequence

- [ ] dry day, ≥8 °C — bitumen won't take otherwise
- [ ] lift felt, check boards for rot, swap the ridge section
- [ ] paint, felt, nails at 50 mm spacing, second paint coat

## Note

The ladder's feet are perished — buy rubber feet first, don't be an
idiot about it. Logged in [home maintenance](../areas/home-maintenance.md).
#house
"""

NOTES["projects/zine-issue-3.md"] = """\
# Zine — issue 3

48 pages A6, "tools people made themselves". Print run 150, same
printer as issue 2. Distro channels are in
[zine distribution](../areas/zine-distribution.md).

## Contents (locked)

- Ana's letterpress — interview done, needs trimming to 6 pp
- Bicycle repair co-op — photos selected
- The seed library — waiting on one quote back
- Centrefold: fold-your-own cord holder pattern

## Production

- [ ] final PDF to printer by Nov 3
- [ ] paper: recycled 100 gsm text / 250 gsm cover, same as issue 2
- [ ] sticker sheet insert — get quote before committing

## Money

Printer quote EUR 2.10/unit at 150 → break even at EUR 5 cover with
typical distro cut. Fine. Tracked loosely under
[finances](../areas/finances.md). #zine
"""

NOTES["projects/zine-distribution.md"] = """\
# Zine distribution

Who carries the zine and on what terms. Moved here from areas —
it's a project concern, not a standing one.

## Stockists

- Penrose Books (town centre): 20 copies, 60/40 split, pays on sale
- Café Lombard: free-pile + honesty box, ~10 copies
- Record shop on Via Roma: wants 15, no terms yet — chase

## Mail

- stamps + rigid mailers in the desk drawer
- post office run Saturdays before 12:30

## Lessons from issue 2

- write the shop name ON the invoice copy, not just the envelope
- honesty box at Lombard earned EUR 31 — do it again
- three copies went to contributors late; set a calendar reminder

See [zine issue 3](zine-issue-3.md) for the current run. #zine
"""

NOTES["projects/vegetable-garden-2027.md"] = """\
# Vegetable garden 2027

Four raised beds, ~9 m² total. Next year's plan built from this
year's failures. Seed sources in [seed catalogue](../resources/seed-catalogue.md).

## What earned its bed

- tomatoes 'San Marzano' — 14 kg from 6 plants
- courgettes — obviously; one plant, not three, learn this
- rocket + cut-and-come-again lettuce, continuous

## What didn't

- sweetcorn: wind flattened it twice, needs the sheltered bed or nothing
- aubergines: flea beetle shredded them — try under mesh or drop

## Calendar

- Feb: start tomatoes + peppers indoors
- Mar: direct sow rocket, peas
- May: everything out after last frost (~May 8 here)

## Kitchen loop

Surplus feeds [cooking](../areas/cooking.md) — passata freezing
weekend is already a tradition. #garden
"""

NOTES["projects/keyboard-build.md"] = """\
# Keyboard build

Alice-layout 65%, because the wrist angle matters more than the
aesthetic. Full BOM in [keyboard parts list](../resources/keyboard-parts-list.md).

## Decisions

- plate: FR4, not alu — softer bottom-out for long writing days
- switches: tactile, 62 g, lubed once and never again (the rabbit
  hole ends here)
- keycaps: MT3 profile if the group-buy ever ships; GMK clone as
  fallback

## Build order

- [ ] stabilisers: clip, lube, band-aid mod
- [ ] solder mill-max sockets — future me wants hotswap
- [ ] flash QMK, test every switch position before closing the case

## Notes

Firmware keymap file lives on the NAS, backed up per
[backup routine](../areas/backup-routine.md). #tech
"""

NOTES["projects/home-network.md"] = """\
# Home network

Local-first setup: nothing depends on a subscription or a datacentre.

## Layout

- Router/AP: consumer unit, stock firmware, no cloud account linked
- NAS: 2-bay mirror, holds photos, scans, the note archive, ISOs
- Pi: runs the internal git remote + a print queue; nothing else —
  resist scope creep

## Rules

- anything irreplaceable exists on the NAS AND an offline disk —
  see [backup routine](../areas/backup-routine.md)
- no IoT that phones home; the two smart plugs are on the guest VLAN
- if a service needs an account to function offline, it doesn't get
  installed — [cli cheat sheet](../resources/cli-cheat-sheet.md) has
  the firewall one-liners

## Open

- [ ] document the Pi image restore steps
- [ ] second offline disk to the parents' house for off-site
#tech #local
"""

NOTES["projects/move-to-valencia.md"] = """\
# Move to Valencia?

Feasibility note — not a decision yet. Timeline if yes: 2028.

## For

- rent ~30% under current for the same space
- language: already working toward it, see
  [language exam](language-exam.md) and [spanish](../areas/spanish.md)
- climate: cycling 11 months a year

## Against / unknown

- freelance paperwork as autónomo — real cost unclear
- visa path if any; EU so likely trivial, confirm
- leaving the studio co-op + kiln access behind

## Research so far

- neighbourhoods: Benimaclet vs Ruzafa vs Cabanyal — Benimaclet leads
- [tax checklist](../resources/tax-checklist.md) has a Spain section
- two scouting trips pencilled into [budget 2027](budget-2027.md)

## Next step

Book the April trip: 10 days, live like a local, work mornings,
cycle afternoons. Then decide. #spain
"""

# ---- areas ------------------------------------------------------------

NOTES["areas/health.md"] = """\
# Health

Standing note. GP: Dr. Ferrer, practise on Carrer Major — annual
bloodwork each January.

## Current

- knees: fine since the squat form fix — see
  [strength training](strength-training.md)
- sleep: the lever that moves everything else —
  [sleep](sleep.md)
- caffeine: none after 13:00, enforced badly

## Baselines (last checkup)

- BP 118/74 · resting HR 54
- ferritin low-normal — keep an eye, red meat + greens, recheck July
- vitamin D insufficient in Feb bloodwork → supplement Oct–Apr

## Appointments

- [ ] dental hygienist — overdue since June
- [ ] eyes — every two years, due March

## Related

[dog training](dog-training.md) is secretly 8k steps a day. #health
"""

NOTES["areas/strength-training.md"] = """\
# Strength training

3x/week full-body, garage barbell. Log template in
[workout log template](../resources/workout-log-template.md).

## Current programme

- A: squat · bench · row
- B: deadlift · press · chin-up (assisted, medium band)
- linear until it stalls, then 5% back-off and re-run

## Numbers (Oct)

- squat 100x5 · bench 62.5x5 · deadlift 140x5 · press 42.5x5

## Rules that keep me injury-free

- deload every 6th week whether it feels needed or not
- film the top set — eyes lie, video doesn't
- knees track over toes; the caving was the pain source all along

## Cross

Bike legs feed [bike tour alps](../projects/bike-tour-alps.md).
Everything rolls up to [health](health.md). #health
"""

NOTES["areas/sleep.md"] = """\
# Sleep

The single highest-leverage habit. Bad weeks always trace back here.

## What works

- out of bed at 6:40, weekends included — consistency over duration
- last coffee 13:00 (see [espresso dialing](../resources/espresso-dialing.md)
  for the ritual I refuse to give up)
- phone charges in the kitchen, alarm is a clock, no exceptions
- cool room: window cracked even in January + heavier duvet

## What doesn't

- "catching up" — a 9 h Sunday wrecks Monday worse than the deficit
- alcohol within 3 h of bed: measurable even at one glass
- late hard training — keep sessions before 19:30

## Metrics

Rough self-score in the daily notes; don't over-instrument this,
the trend line is what matters. Ties into [health](health.md). #health
"""

NOTES["areas/finances.md"] = """\
# Finances

Standing rules + account map. The year-by-year numbers live in
[budget 2026](../projects/budget-2026.md).

## Rules

- pay myself first: transfer on the 1st, not "what's left"
- no consumer debt; credit card paid in full, used only for travel
  insurance
- cash buffer for the pop-up / studio kept separate —
  [sourdough pop-up](../projects/sourdough-pop-up.md)

## Accounts

- main bank: income in, bills standing order on the 3rd
- savings: instant access, emergency fund only
- broker: boring index fund, quarterly buys, never checked in between

## Paperwork

Annual ritual in [tax checklist](../resources/tax-checklist.md).
Everything as PDFs on the NAS, mirrored per
[backup routine](backup-routine.md). #finance
"""

NOTES["areas/home-maintenance.md"] = """\
# Home maintenance

Running list for the flat + shed + garage. Urgent-now: the
[shed roof](../projects/shed-roof.md).

## Seasonal

- Oct: gutters, bleed radiators, test the CO alarm
- Feb: service the boiler (landlord arranges, I nag)
- Apr: oil door hinges, check sealant around bath + windows
- Jun: descale the kettle path — espresso machine schedule is in
  [espresso dialing](../resources/espresso-dialing.md)

## Ongoing niggles

- bathroom extractor fan rattles above ~60%
- sash window in study sticks — probably just paint buildup
- shed felt (see project note)

## Car

Separate note: [car maintenance](car-maintenance.md). Garage build-out
tracked in [garage workshop](../projects/garage-workshop.md). #house
"""

NOTES["areas/cooking.md"] = """\
# Cooking

Home base for the recipes that survived repetition. Full cards in
resources: [sourdough](../resources/recipes-sourdough.md),
[miso soup](../resources/recipes-miso-soup.md),
[pasta](../resources/recipes-pasta.md).

## Rotation

- Sun: big pot — ragù, bean stew, or curry; lunches sorted to Wed
- Wed: noodles or [miso soup](../resources/recipes-miso-soup.md) — fast
- Fri: pizza night, dough Thursday evening

## Garden loop

Summer surplus from
[vegetable garden 2027](../projects/vegetable-garden-2027.md):
passata weekend in late August, pesto freezes flat in bags.

## Skills queue

- [ ] proper tortilla — the flip, every time, without the plate trick
- [ ] laminated dough once, just to know I can
- [x] stock from roast carcass — never buy cubes again
#food
"""

NOTES["areas/moss-terrarium.md"] = """\
# Moss terrarium

Renamed from "moss pole" — it grew past the pole into a sealed jar
project. One of the [houseplants](houseplants.md), sort of.

## Build log

- 2026-07: kilner jar, gravel layer, charcoal, sphagnum, then cushion
  moss from the north wall (legal: own garden)
- 2026-08: condensation dialed in — lid cracked 2 mm on warm days
- 2026-09: added a tiny fern; may regret it, ferns spread

## Care

- light: bright indirect, never direct sun — jar becomes an oven
- water: ~20 ml/month via syringe at the edge
- if glass stays fogged >24 h, open the lid for a day

## Why

It's the only plant system I've kept alive >1 year entirely
untouched, which says something about my other plants. #plants
"""

NOTES["areas/houseplants.md"] = """\
# Houseplants

Inventory + the watering truth table, because "once a week" killed
three plants in 2025.

## Survivors

- pothos (x3): indestructible, propagate cuttings for gifts
- sansevieria: ignore it, it prefers that
- monstera: moss pole in name only — actual moss project is the
  [moss terrarium](moss-terrarium.md)
- two orchids: ice-cube method is a myth; soak-and-drain instead

## Schedule (what actually works)

- stick a finger in; water only when dry to the second knuckle
- feed weakly weekly April–Sep, nothing Oct–Mar
- all pots get drainage or the plant gets composted — no cachepots

## Wishlist

- hoya carnosa for the bathroom shelf
- a real monstera deliciosa once the Valencia question
  ([move to valencia](../projects/move-to-valencia.md)) resolves —
  no point buying a plant I might move countries away from
#plants
"""

NOTES["areas/car-maintenance.md"] = """\
# Car maintenance

2011 Golf, 148k km, worth more to me working than anything I'd
replace it with.

## Log

- 2026-04: oil + filter (DIY), air filter, wiper blades
- 2026-07: MOT passed, advisory on front brake lines' corrosion —
  budget for lines + fluid next spring
- 2026-09: tyre rotation; rears have ~3 mm, fronts ~4.5 mm

## Schedule

- oil every 10k or yearly, whichever first
- cambelt due 2027 or 180k — this is THE non-negotiable one
- winter check in the [home maintenance](home-maintenance.md)
  October block: battery, antifreeze, tyres

## Costs

Track under "car" in [finances](finances.md); ~EUR 900/yr all-in
excluding fuel. Still cheaper than anything newer. #house
"""

NOTES["areas/dog-training.md"] = """\
# Dog training

Bramble, 3-year-old rescue lurcher. Reactive to skateboards,
otherwise a couch ornament.

## Current work

- LAT (look-at-that) for skateboard/bike triggers — 15 m threshold,
  shrinking slowly
- loose-lead: heel position with scattered food drops, 10 min/day
- recall: only on the long line; she has selective hearing bred in

## Log highlights

- 2026-08: passed a skateboard at 20 m without a bark — jackpot
- 2026-09: regression week after the bin-truck incident; back to
  25 m and it's fine, setbacks aren't linear

## Routine

- 7:00 walk 40 min (the real exercise, see [health](health.md))
- 17:30 walk 30 min + 10 min training
- Sat: long hike — sometimes doubles as
  [bike tour](../projects/bike-tour-alps.md) route scouting
#dog
"""

NOTES["areas/backup-routine.md"] = """\
# Backup routine

The 3-2-1 thing, done with local hardware. Covers everything on the
NAS including the [family photos](../projects/family-photos.md)
project and this notes repo.

## Layout

- primary: NAS mirror (2x4 TB)
- local copy: USB disk, weekly rsync, Sunday evening
- off-site: second USB disk at parents', swapped on visits
  (~every 6–8 weeks)

## Schedule

- Sun 21:00: `rsync -a --delete nas/ usb/` — commands in
  [cli cheat sheet](../resources/cli-cheat-sheet.md)
- monthly: verify by reading back a random folder, not just trusting
  rsync's exit code
- quarterly: spin up the off-site disk and check SMART

## Tested restores

- 2026-05: restored the whole notes dir from USB after a fat-fingered
  `rm -rf` — 4 min, everything back. Worth every habit above.
#tech #local
"""

NOTES["areas/spanish.md"] = """\
# Spanish

Standing practice note; exam structure lives in
[language exam](../projects/language-exam.md), motivation in
[move to Valencia](../projects/move-to-valencia.md).

## Daily minimum (30 min)

- Anki: 15 min, shared deck + own cards from reading
- input: one [podcast](../resources/podcasts.md) episode or 10 pages
  of the graded reader
- Thu: intercambio call with Lucia — her English, my Spanish, 30/30

## Trouble list

- subjunctive triggers — finally sticking after the lyrics drill
- por/para: still 80/20 wrong in speech, fine in writing
- numbers above 100 said fast: pure panic, drill weekly

## Wins

- held a 25-min conversation about sourdough — turns out
  [recipes](../resources/recipes-sourdough.md) vocabulary transfers
- understood a film without subtitles for 40 min straight (Aug)
#spanish
"""

# ---- resources --------------------------------------------------------

NOTES["resources/books-to-read.md"] = """\
# Books to read

Queue is deliberately short — it grows faster than I read.

## Now

- *The Creative Act* — Rubin (borrowed, due back; notes when done go
  in this folder like [book notes design](../resources/book-notes-design-of-everyday-things.md))

## Next

- *Braiding Sweetgrass* — Kimmerer
- *The Dispossessed* — Le Guin (see
  [book notes four thousand weeks](book-notes-four-thousand-weeks.md)
  for the time-systems thread)
- *Pattern Language* — Alexander et al., skim not read
- *La sombra del viento* — Zafón, the Spanish stretch goal
  ([spanish](../areas/spanish.md))

## Done (recent)

- *The Design of Everyday Things* — notes linked above
- *Four Thousand Weeks* — notes linked above
- *The Overstory* — no notes, just feelings

## Rule

Three unpurchased max on the queue; want-list lives in a text file on
the NAS instead. #reading
"""

NOTES["resources/book-notes-design-of-everyday-things.md"] = """\
# Book notes: The Design of Everyday Things

Norman. Read Feb 2026. Kept because it rewired how I look at the
studio's "how to use this mug" complaints.

## Takeaways

- affordance vs signifier: the door that says PULL but affords push
  is a design failure, not a user failure
- knowledge in the world vs in the head — the [cli cheat sheet](cli-cheat-sheet.md)
  exists because flags are knowledge-in-the-head I'd rather offload
- forcing functions: the NAS refusing unmounted writes saved me once

## Applied

- workshop layout: frequently-used tools at arm height —
  [garage workshop](../projects/garage-workshop.md)
- the zine's fold pattern is deliberately discoverable —
  [zine issue 3](../projects/zine-issue-3.md)

## Quotes

- "It is not your fault." — print it on the studio wall
#reading
"""

NOTES["resources/book-notes-four-thousand-weeks.md"] = """\
# Book notes: Four Thousand Weeks

Burkeman. Read Jun 2026. The anti-productivity productivity book.

## Takeaways

- finitude as relief: the inbox is infinite by design; choosing what
  to drop IS the job, not a failure of throughput
- the "efficiency trap" — processing email faster only earns more email
- becoming a better procrastinator: pick what to neglect on purpose
  (mine: social feeds, most news, keeping up with software churn)

## Applied

- daily notes list three things max, not fifteen
- the [budget 2027](../projects/budget-2027.md) "no new commitments
  without dropping one" rule came straight from this
- stopped tracking reading speed — the queue note
  ([books to read](books-to-read.md)) is capped instead

## Counterpoint held loosely

Some of it is permission disguised as wisdom; deadlines on the zine
still exist. #reading
"""

NOTES["resources/recipes-sourdough.md"] = """\
# Sourdough, house loaf

The one recipe I could write from memory — kept here anyway because
percentages drift.

## Formula (2 loaves)

- 1000 g bread flour
- 720 g water (72%)
- 200 g levain (100% hydration)
- 20 g salt

## Schedule

- 21:00 feed starter
- 07:30 mix, autolyse 45 min
- folds at +45/+45/+60/+60
- bulk ~4.5 h at 23 °C — watch the dough, not the clock
- shape, cold retard overnight, bake 230 °C lid-on 20 / lid-off 22

## Notes

- starter lives at 100% rye refresh once a week, counter otherwise
- doubled version fuels [sourdough pop-up](../projects/sourdough-pop-up.md)
- summer bulk is 1 h shorter — write it down EVERY year and still
  forget it every June

Part of [cooking](../areas/cooking.md). #food
"""

NOTES["resources/recipes-miso-soup.md"] = """\
# Miso soup (the weeknight one)

Fifteen minutes, clears the fridge.

## Base

- 750 ml dashi — powder is fine, own it
- 3 tbsp miso (awase), never boiled — off heat, ladle-dissolve
- wakame, small pinch, rehydrated 5 min
- tofu, half a block, cubed

## Variations that happened

- + sliced mushrooms (shiitake > button)
- + leftover roast squash, cubed — surprisingly good
- + a beaten egg drizzled in — egg-drop miso, do it again

## Rules

- miso in last, heat off, no exceptions
- spring onion on top, always
- serve with rice and the leftover situation resolves itself

Rotation anchor for Wednesdays, see [cooking](../areas/cooking.md).
Pairs suspiciously well with the garden's greens glut —
[vegetable garden 2027](../projects/vegetable-garden-2027.md). #food
"""

NOTES["resources/recipes-pasta.md"] = """\
# Pasta — cacio e pepe

The three-ingredient trap: simple list, real technique.

## Ingredients (2)

- 320 g tonnarelli or thick spaghetti
- 90 g pecorino romano, microplaned fine
- black pepper, ~2 tsp, toasted and crushed coarse

## Method

- toast pepper in a dry pan, add a ladle of pasta water, bloom it
- cook pasta in LESS water than instinct says — you need it starchy
- pecorino + pasta water off-heat into a paste FIRST, in a bowl
- pasta to 80% done, finish in the pan, then into the cheese bowl
- toss violently; add water a spoon at a time until it glosses

## Failure modes (all personally verified)

- cheese clumps = pan or bowl too hot
- soup at the bottom = too much water, toss longer
- bland = supermarket "parmesan" is not pecorino

Filed under [cooking](../areas/cooking.md). #food
"""

NOTES["resources/keyboard-parts-list.md"] = """\
# Keyboard parts list

BOM for the build in [keyboard build](../projects/keyboard-build.md).

## Bought

- case + PCB kit (Alice65 clone): EUR 118
- FR4 plate: included
- mill-max sockets x70: EUR 22

## Pending

- switches: waiting on the tactile group-buy; fallback is buying
  70 Gazzews outright
- keycaps: MT3 group-buy ships "Q1" (they always do); fallback set
  identified at EUR 89
- stabilisers: 4x 2u + 1x 6.25u wire — order with the switches

## Already owned

- soldering iron, lube kit from the last build
- switch opener (lost and found twice — now tethered to the toolbox)

## Budget

Cap EUR 300 total; currently 140 in. Logged against "studio" or
"hobby"? — hobby, be honest, per [finances](../areas/finances.md).
#tech
"""

NOTES["resources/packing-list.md"] = """\
# Packing list — general

Base layer for any trip; the cycling version adds spares via
[bike tour checklist](../projects/bike-tour-checklist.md) and the tour
itself is [bike tour alps](../projects/bike-tour-alps.md).

## Always

- passport + photocopy, EHIC
- one packing cube: 4 days of clothes regardless of trip length,
  laundry exists
- toiletries + meds + plasters
- book + paper notebook (this system, pocket size)
- charger + EU adapter + a real cable not the short freebie

## Trip-type adds

- hiking: poles, blister kit, filter bottle
- city: nothing extra — cities have shops
- work: laptop + the SSD with a recent NAS sync, see
  [backup routine](../areas/backup-routine.md)

## After-action note

Every trip, delete what didn't get used. The list used to be two
pages. #travel
"""

NOTES["resources/cli-cheat-sheet.md"] = """\
# CLI cheat sheet

Commands I keep re-googling; now I re-grep instead.

## Files

- `rsync -a --delete src/ dst/` — mirror; trailing slashes matter
- `rsync -an --delete src/ dst/` — dry run first, always
- `find . -name '*.md' -newer marker.txt` — changed since
- `du -sh */ | sort -h` — who's eating the disk

## Text

- `grep -rn "pattern" .` — the whole toolbox
- `sed -n '10,20p' file` — print a range
- `sort | uniq -c | sort -rn` — instant histogram

## System

- `python3 -m http.server 8000` — local preview (used by
  [studio website](../projects/studio-website.md))
- `systemctl --user status x` / `journalctl --user -u x -f`
- `sudo ufw status numbered` / `sudo ufw deny out to any` variants —
  firewall one-liners referenced from [home network](../projects/home-network.md)

## rsync-to-NAS

The Sunday backup command block lives in
[backup routine](../areas/backup-routine.md). #tech
"""

NOTES["resources/markdown-notes.md"] = """\
# Markdown notes

How this whole notes folder is wired — conventions, not opinions.

## Links

- one convention only: relative markdown links —
  [like this one to health](../areas/health.md) — resolved relative
  to the note's folder
- no wiki-links (`[[...]]`), no absolute paths, no bare filenames —
  mixed conventions broke things once, once was enough
- link targets end in `.md`; headings are not linked, files are

## Tags

- `#tag` inline, lowercase, one or two per note max — they're for
  the tag index, not decoration
- folders carry the real structure; tags are just threads

## Files

- filename: lowercase-with-dashes.md, the title is the `#` heading
- daily notes: `YYYY-MM-DD.md` in `daily/`, freeform
- when a note outgrows its folder, split it — don't nest folders

## History

The whole tree is a git repo synced to the Pi — see
[home network](../projects/home-network.md) and
[git cheat sheet](git-cheat-sheet.md). #tech
"""

NOTES["resources/git-cheat-sheet.md"] = """\
# Git cheat sheet

The six commands that cover 95% of what I do, plus the Pi remote
setup that pairs with [the home netwrok](../projects/home-netwrok.md).

## Daily

- `git status` / `git diff` / `git add -p` — stage hunks, not files
- `git log --oneline -15` — recent history
- `git stash` / `git stash pop` — rarely but precious

## Fixes

- `git checkout main -- path/` — restore a tracked subtree, e.g.
  after mangling generated files
- `git reset --soft HEAD~1` — uncommit, keep the work
- `git reflog` — the actual undo button

## Pi remote

- `git push pi main` — the NAS-adjacent mirror
- clone from it on a new machine; offline-first per
  [backup routine](../areas/backup-routine.md)

## Rule

Commit small, message in imperative, and never keep work uncommitted
overnight on a machine with one disk. #tech
"""

NOTES["resources/podcasts.md"] = """\
# Podcasts

Offline queue — episodes downloaded ahead, mostly for walks and the
language grind ([spanish](../areas/spanish.md)).

## Spanish

- *Hoy Hablamos* — daily 12-min episodes, right at B1 level
- *Radio Ambulante* — stretch listening, transcripts exist
- *Español con Juan* — when the brain is tired, he's gentle

## Long-form

- *The Rest Is History* — the backlog is a lifestyle
- *Darknet Diaries* — keeps the [home network](../projects/home-network.md)
  paranoia calibrated
- *99% Invisible* — pairs with the design bent in
  [book notes design](book-notes-design-of-everyday-things.md)

## Rules

- 1.25x max; faster is just skimming
- queue maxes at 30 episodes, then oldest get deleted unheard —
  borrowed from [book notes four thousand weeks](book-notes-four-thousand-weeks.md)
#reading
"""

NOTES["resources/glaze-recipes.md"] = """\
# Glaze recipes

Tested combos for the stoneware body. Fired to cone 6 oxidation at
Clayworks — see [ceramics studio](../projects/ceramics-studio.md).

## Keepers

- "oat" — 40 feldspar / 25 silica / 20 whiting / 15 kaolin + 6 rutile:
  breaks tan over the throwing rings
- "basalt" — above + 8 black iron oxide: near-black, satin
- shino commercial mix: thin, or it crawls — two quick dips max

## Tests queued

- oat + 2% cobalt wash on the rim only
- wood-ash experiment when the shed burn happens —
  [shed roof repair](../projects/shed-roof-repair.md) generates the
  raw material, darkly funny

## Failures file

- anything over 10% rutile = muddy brown regret
- layered shino-over-basalt blisters at cone 6 — stop trying it
#ceramics
"""

NOTES["resources/seed-catalogue.md"] = """\
# Seed catalogue

Suppliers + the running want-list for
[vegetable garden 2027](../projects/vegetable-garden-2027.md).

## Suppliers

- Real Seeds (Wales): open-pollinated, generous packets, good notes
- Franchi (Italy): cheap volume, the rocket + radicchio source
- local seed library: swap table each March — see
  [zine issue 3](../projects/zine-issue-3.md), they're a featured story

## Order list (Feb)

- tomato 'San Marzano 2' x1 — proven
- courgette 'Romanesco' x1 — ONE plant
- pea 'Kelvedon Wonder'
- radicchio 'Treviso' — new this year
- aubergine: skip unless the mesh arrives

## Storage

Seeds in the fridge door, silica packets refreshed yearly. Viability
drops fast above ~10 °C. #garden
"""

NOTES["resources/workout-log-template.md"] = """\
# Workout log template

Copy-paste per session into the daily note; programme in
[strength training](../areas/strength-training.md).

## Template

    ## training
    - squat 3x5 @ ___kg — rpe _
    - bench 3x5 @ ___kg — rpe _
    - row 3x5 @ ___kg — rpe _
    - notes: sleep _/10, warmup ok?

## Rules

- log immediately, memory inflates rpe downward
- missed lifts get the same note, never deleted — the pattern of
  misses is the useful part
- deload weeks get logged too (5% off, same reps)

## History

Logs since 2026-01 live inside the dated daily notes; grep finds
them fine — see [cli cheat sheet](cli-cheat-sheet.md). #health
"""

NOTES["resources/espresso-dialing.md"] = """\
# Espresso dialing-in

Gaggia Classic + hand grinder. The ritual before the 13:00 cutoff
in [sleep](../areas/sleep.md).

## Baseline

- 18 g in, 36 g out, ~28–32 s including preinfusion
- temp: 93 °C — the stock thermostat needs the surf trick
- water: filtered, not distilled — flat water, flat shot

## Dialing a new bag

- start at last bag's grind ±0 — it's never ±0
- sour/thin → finer or hotter; bitter/dry → coarser or cooler
- change ONE thing per shot; two changes = no information

## Maintenance

- backflush weekly, descale monthly (schedule in
  [home maintenance](../areas/home-maintenance.md))
- group gasket replaced 2026-03 — next due ~2027

## Beans

Roaster in town, 250 g bags, roast date on the label or it doesn't
come home. Logged vaguely under [finances](../areas/finances.md)
"food". #food
"""

NOTES["resources/tax-checklist.md"] = """\
# Tax checklist

Freelance + PAYE mix. Files under [finances](../areas/finances.md);
Spain-specific unknowns feed [move to Valencia](../projects/move-to-valencia.md).

## Annual (deadline end of Jan)

- [ ] export all bank CSVs, dedupe
- [ ] invoices folder — every numbered invoice PDF'd
- [ ] deductible kit: studio share of utilities, materials receipts,
  market stall fees (see [ceramics studio](../projects/ceramics-studio.md))
- [ ] file; set a reminder mid-December because January sneaks

## Quarterly

- VAT not applicable under threshold — recheck if ceramics revenue
  crosses it
- pension contribution before year-end for the relief

## Spain section (research)

- autónomo cuota: sliding scale, ~EUR 80–300/mo, needs real numbers
- digital certificate + tax residency at 183 days — confirm, don't
  assume
#finance
"""

NOTES["resources/film-photography.md"] = """\
# Film photography

One camera, two stocks, slow deliberate pace. Scans feed
[family photo archive](../projects/family-photo-archive.md) storage.

## Kit

- Olympus OM-1 + 50/1.8 — light-seal replaced 2025
- stocks: HP5 pushed to 800 indoors, Ektar for trips

## Workflow

- dev at the lab in town — home dev is a no in a rental
- scan on the flatbed: 2400 dpi JPEG is plenty for 6x4s and zines
- negatives filed in binders by roll, sleeve sheet labelled
  `roll-NY`

## Projects

- [ ] a photo essay per market day — pairs with
  [ceramics studio](../projects/ceramics-studio.md) documentation
- [ ] the Alps roll — two Ektar + one HP5 reserved for
  [bike tour alps](../projects/bike-tour-alps.md)
#photo
"""

# ---- daily ------------------------------------------------------------

DAILY = {}

DAILY["2026-08-17.md"] = """\
# 2026-08-17 — Mon

Quiet re-entry after the week at the coast.

## today

- inbox-zero on the paper pile: recycled a stack, filed three things
- studio: trimmed the Thursday mugs — see
  [ceramics studio](../projects/ceramics-studio.md)
- walked Bramble along the river; she ignored two bikes entirely —
  progress per [dog training](../areas/dog-training.md)

## notes

- tomato glut starting already — first passata thought of the year
  per [vegetable garden](../projects/vegetable-garden-2027.md)
- idea: a "seconds shelf" at the market instead of a bin #idea

## three things

- [x] flatbed scanner unpacked and tested
- [ ] call the timber merchant about the felt
- [ ] book Lucia's intercambio slot for Thursday
"""

DAILY["2026-08-20.md"] = """\
# 2026-08-20 — Thu

Intercambio day.

## today

- Lucia call: 30/30 as usual — talked about markets, used "puesto"
  wrong twice, she wrote it down for me
- scanned 40 prints from box 2 — the [photo archive](../projects/family-photo-archive.md)
  is actually moving
- deadlift day, 135x5 felt honest — [strength training](../areas/strength-training.md)

## notes

- the flatbed's crop marks only work on the glass edge, note for the
  process section
- R.'s letterpress intro confirmed for next week — zine content

## three things

- [x] intercambio
- [x] scan session
- [ ] glaze test tiles labelled (the rutile one especially)
"""

DAILY["2026-08-24.md"] = """\
# 2026-08-24 — Mon

Hot. Garage is an oven — workshop work pauses itself.

## today

- seed order drafted for Feb, sat on it rather than sending —
  see [seed catalogue](../resources/seed-catalogue.md)
- sorted the spice drawer, the small win of the day
- evening: zine layout sketches for the co-op spread —
  [zine issue 3](../projects/zine-issue-3.md)

## notes

- courgette count: 11 and climbing. One plant next year. Written.
- found the missing switch opener — it was in the seed tin, don't ask

## three things

- [x] seed list draft
- [x] zine spread sketch
- [ ] water the moss jar (it hit 30 °C in there)
"""

DAILY["2026-08-30.md"] = """\
# 2026-08-30 — Sun

Backup Sunday + a small script victory.

## today

- weekly rsync done, verified by actually opening files this time —
  ritual per [backup routine](../areas/backup-routine.md)
- wrote a tiny tag-count helper with the commands from
  [the cli sheet](../resorces/cli-cheat-sheet.md) — felt clever,
  then remembered that file exists precisely for this
- miso soup for dinner; used the roast-squash variation —
  [recipe](../resources/recipes-miso-soup.md)

## notes

- off-site disk swap next parents' visit — put it in the calendar now
- the Pi's SD card is 3 years old; clone it before it dies, not after

## three things

- [x] backup verified
- [x] tag helper script
- [ ] order SD card
"""

DAILY["2026-09-02.md"] = """\
# 2026-09-02 — Wed

Bakery planning call with Marta.

## today

- pop-up date set: Oct 17, her café's front table —
  [sourdough pop-up](../projects/sourdough-pop-up.md)
- test bake this weekend: the seeded rye needs a schedule, it drags
- evening class prep — past paper listening, 19/25, the numbers
  section still kills me — [language exam](../projects/language-exam.md)

## notes

- Marta can lend a second proving basket; bring flour as rent
- honesty-box pricing: keep EUR 7/8.50, round numbers help the till

## three things

- [x] date + menu locked
- [ ] rye test bake Sat
- [ ] print the sign-up sheet draft
"""

DAILY["2026-09-06.md"] = """\
# 2026-09-06 — Sun

Long ride day.

## today

- 88 km with the full bag as a shakedown — knees fine, hands numb at
  km 70, bar tape or gloves, decide — [bike tour alps](../projects/bike-tour-alps.md)
- post-ride inventory against [the checklist](../projects/bike-tour-checklist.md):
  missing chain links + second bottle cage
- roast chicken, stock on for the week — [cooking](../areas/cooking.md)

## notes

- Flexenpass gradient math: 10 km at ~7% — doable at current fitness,
  keep the loaded weekends coming
- new gloves ordered, EUR 18, logged

## three things

- [x] shakedown ride
- [x] gear gap list
- [ ] frame bag strap: one side is fraying, replace
"""

DAILY["2026-09-09.md"] = """\
# 2026-09-09 — Wed

Rain day, desk day.

## today

- zine interviews trimmed: Ana's letterpress down to 6 pages and
  better for it — [zine issue 3](../projects/zine-issue-3.md)
- Anki + podcasts only, no energy for grammar —
  [spanish](../areas/spanish.md)
- reorganised the notes folder README — wrote down the one-link-
  convention rule finally ([markdown notes](../resources/markdown-notes.md))

## notes

- distribution terms question to Penrose: raise the split to 65/35?
  next drop-off — [zine distribution](../projects/zine-distribution.md)
- daily notes keep growing a "three things" tail; keep it, it works

## three things

- [x] letterpress edit
- [ ] chase the seed-library quote (2nd time)
- [ ] sticker sheet quote request email
"""

DAILY["2026-09-14.md"] = """\
# 2026-09-14 — Mon

Garden-turning-to-autumn day.

## today

- repotted the fern out of the jar — it was winning against the
  [moss pole](../areas/moss-pole.md), which needed the space
- passata weekend done: 9 jars from the San Marzanos —
  [vegetable garden](../projects/vegetable-garden-2027.md) tally
  updated
- cleaned the espresso group's screen while everything dried —
  [espresso dialing](../resources/espresso-dialing.md)

## notes

- terrarium reads better with just moss + one stone; restraint is
  the whole aesthetic — see also [houseplants](../areas/houseplants.md)
- 14 kg tomatoes from 6 plants confirmed; that's the number for the
  2027 plan

## three things

- [x] passata batch
- [x] fern eviction
- [ ] label jars properly this time (year + batch)
"""

DAILY["2026-09-18.md"] = """\
# 2026-09-18 — Fri

Budget review, slightly painful.

## today

- monthly reconcile: groceries running +9% on the year — the 2027
  number needs to be honest, see [budget 2027](../projects/budget-2027.md)
- parked the keyboard keycap group-buy decision another month —
  [keyboard build](../projects/keyboard-build.md) can wait
- three pages of the Zafón — slow but real —
  [books to read](../resources/books-to-read.md)

## notes

- "studio" as its own category already feels right; the materials
  spend was invisible before
- emergency fund at 4.2 months — on pace

## three things

- [x] reconcile
- [ ] insurance requote reminder for March
- [ ] cancel the duplicated cloud storage (it's empty anyway)
"""

DAILY["2026-09-22.md"] = """\
# 2026-09-22 — Tue

Shed day, half done.

## today

- ladder rubber feet arrived; shed patch now unblocked —
  [shed roof repair](../projects/shed-roof-repair.md)
- scraped the lifting felt edge and found the rot is only surface —
  patch will hold to spring
- Bramble to the vet for boosters; she was magnificent and then
  sulked — [dog training](../areas/dog-training.md)

## notes

- timber merchant had the ridge board in stock — one trip next
  Saturday if the forecast holds
- the fan rattle in the bathroom is worse; moved it up the
  [home maintenance](../areas/home-maintenance.md) list

## three things

- [x] shed assessed
- [x] vet
- [ ] bitumen paint + clout nails shopping list to Sat
"""

DAILY["2026-09-25.md"] = """\
# 2026-09-25 — Fri

Rye test bake, qualified success.

## today

- seeded rye at 60/40: crumb good, schedule too long — needs a
  warmer bulk or more levain; notes appended to the
  [sourdough recipe](../resources/recipes-sourdough.md) margin
- pop-up flyers hand-lettered, scanned, off to Marta for printing —
  [sourdough pop-up](../projects/sourdough-pop-up.md)
- pressed the record on guitar-callus annoyance; irrelevant but
  chronicled

## notes

- 60-loaf math: 3 oven cycles Friday night + 4 Saturday morning —
  tight but workable
- bag pre-order idea: paper sign-up at the café, test next week

## three things

- [x] rye test
- [x] flyer scan
- [ ] buy the seeds for the topping (sunflower + linseed)
"""

DAILY["2026-09-29.md"] = """\
# 2026-09-29 — Tue

Printers and proofs.

## today

- zine printer quote in: EUR 2.10/unit at 150 — inside the break-even
  math in [zine issue 3](../projects/zine-issue-3.md)
- proof pages back: centrefold registration is off by ~2 mm; re-export
  with more bleed
- strength session moved to lunch — evening slots keep colliding
  with intercambio — [strength training](../areas/strength-training.md)

## notes

- Penrose will take 25 copies this run — up from 20
- the quote back from the seed library finally arrived; 4 months
  late, still printing it

## three things

- [x] printer quote logged
- [ ] re-export centrefold tonight
- [ ] mailer count — need ~40, have ~20
"""

DAILY["2026-10-02.md"] = """\
# 2026-10-02 — Fri

New coffee bag day.

## today

- fresh beans from the roaster — dialed in over three shots, landed
  at grind 11 vs last bag's 13; logged in
  [espresso](../resources/espresso.md)
- boiler service booked for Nov 4 (the landlord moved, finally) —
  also noted in [home maintenance](../areas/home-maintenance.md)
- evening: Valencia browsing — Benimaclet flats still ~30% under —
  [move to Valencia](../projects/move-to-valencia.md)

## notes

- shot 2 was the good one: 18→37 g in 29 s, hazelnut + orange
- caffeine cutoff held: last sip 12:58, smug

## three things

- [x] beans dialed
- [x] boiler booked
- [ ] April flights — set a price alert the manual way (check Fridays)
"""

DAILY["2026-10-05.md"] = """\
# 2026-10-05 — Mon

Workshop day before the weather turns.

## today

- epoxy floor paint coat 1 in the garage — dry by morning, coat 2
  Tuesday — [garage workshop](../projects/garage-workshop.md)
- timber merchant run: ridge board + felt + nails secured for the
  [shed roof repair](../projects/shed-roof-repair.md)
- threw 6 mug blanks while the floor dried anyway — wheel's in the
  kitchen for now, per [ceramics studio](../projects/ceramics-studio.md)

## notes

- French cleat rail measurement: 240 cm across the back wall
- mud on the kitchen floor; Bramble framed for it, she was innocent

## three things

- [x] epoxy coat 1
- [x] timber run
- [ ] mug blanks: trim Wednesday
"""

DAILY["2026-10-08.md"] = """\
# 2026-10-08 — Thu

Subjunctive breakthrough day, apparently.

## today

- intercambio: drilled "quiero que + subj" with song lyrics — it
  finally clicked, logged in [spanish](../areas/spanish.md)
- mock exam reading section: 21/25 — the format's the thing now, not
  the language — [language exam](../projects/language-exam.md)
- ordered the print-shop mailers for [zine distribution](../projects/zine-distribution.md)

## notes

- tutor for April speaking block: Lucia's friend Marta (different
  Marta, confusingly) — EUR 18/session, book 6
- film without subtitles: held 40 minutes again — real progress

## three things

- [x] intercambio
- [x] mock section
- [ ] write the two timed letters this weekend
"""

DAILY["2026-10-10.md"] = """\
# 2026-10-10 — Sat

Pop-up sign-up sheet live at the café.

## today

- 11 pre-orders on the sheet in one morning — Marta texted a photo —
  [sourdough pop-up](../projects/sourdough-pop-up.md)
- baked the practice run: 4 loaves, consistent, crumb right —
  recipe card unchanged in [recipes](../resources/recipes-sourdough.md)
- long walk with Bramble scouting the river path as a possible tour
  day-1 alternative — [bike tour alps](../projects/bike-tour-alps.md)

## notes

- pre-orders at this rate sell out the 60 — start a waitlist column
- river path is fine but adds 14 km; keep Bregenz start

## three things

- [x] practice bake
- [x] sign-up sheet delivered
- [ ] bags + labels order — last cost line open
"""

DAILY["2026-10-12.md"] = """\
# 2026-10-12 — Mon

First properly cold morning.

## today

- radiators bled, CO alarm tested, gutter check half-done before the
  ladder said no — [home maintenance](../areas/home-maintenance.md)
- swapped to the winter duvet; sleep quality already better —
  [sleep](../areas/sleep.md)
- zine centrefold re-exported with proper bleed — off to the printer
  tomorrow — [zine issue 3](../projects/zine-issue-3.md)

## notes

- gutter done to the back corner only; the conservatory roof blocks
  the last 3 m — roofer quote or clever ladder feet?
- espresso machine descale due — October block per the sheet

## three things

- [x] rads + alarm
- [ ] descale the machine
- [ ] roofer quote for the awkward gutter stretch
"""

DAILY["2026-10-14.md"] = """\
# 2026-10-14 — Wed

Photo day.

## today

- flatbed session: box 2 finished, 214 + 187 scans — naming held up
  per the [family photo archive](../projects/family-photo-archive.md)
  scheme
- shot the studio mugs between cloud gaps — north window only, per
  the [studio website](../projects/studio-website.md) note
- evening: pasta cacio e pepe, nailed it second try this month —
  [recipe](../resources/recipes-pasta.md)

## notes

- aunt Rosa weekend: book it before December or the who-is-who pass
  on box 3 slips to spring
- the OM-1's light meter is jumpy under 1/15 — test roll needed
  before the Alps, see [film photography](../resources/film-photography.md)

## three things

- [x] box 2 done
- [x] mug shoot
- [ ] text Rosa dates
"""

# --------------------------------------------------------------- fixes
# Ground truth for the planted broken links: (file, literal target) ->
# the literal target it should be. Every broken link in the corpus must
# appear here; the generator asserts the scan matches this table.

FIXES = {
    ("daily/2026-08-30.md", "../resorces/cli-cheat-sheet.md"):
        "../resources/cli-cheat-sheet.md",
    ("daily/2026-09-14.md", "../areas/moss-pole.md"):
        "../areas/moss-terrarium.md",
    ("daily/2026-10-02.md", "../resources/espresso.md"):
        "../resources/espresso-dialing.md",
    ("projects/bike-tour-alps.md", "../resources/bike-tour-checklist.md"):
        "bike-tour-checklist.md",
    ("projects/zine-issue-3.md", "../areas/zine-distribution.md"):
        "zine-distribution.md",
    ("areas/finances.md", "../projects/budget-2026.md"):
        "../projects/budget-2027.md",
    ("areas/home-maintenance.md", "../projects/shed-roof.md"):
        "../projects/shed-roof-repair.md",
    ("areas/backup-routine.md", "../projects/family-photos.md"):
        "../projects/family-photo-archive.md",
    ("resources/git-cheat-sheet.md", "../projects/home-netwrok.md"):
        "../projects/home-network.md",
    ("projects/ceramics-studio.md", "../resources/glaze-notebook.md"):
        "../resources/glaze-recipes.md",
}

# ------------------------------------------------------------------ lib

LINK_RE = re.compile(
    r"!?\[[^\]\n]*\]\(\s*(?:<([^>]+)>|([^)\s]+))(?:\s+[^)]*)?\)")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
NORM_RE = re.compile(r"(\]\()\s*(?:<[^>]+>|[^)\s]+)((?:\s+[^)]*)?\))")


def link_target(m):
    return m.group(1) if m.group(1) is not None else m.group(2)


def resolve(root, src, target):
    """Resolve a markdown link target per the KB convention.

    ``root`` is the tree the link must stay inside, ``src`` the absolute
    path of the linking file. Returns the path relative to root if it
    exists on disk, else None. External (scheme/mailto) and pure-anchor
    links return "skip"."""
    t = target.split("#", 1)[0]
    if not t:
        return "skip"
    if SCHEME_RE.match(t):
        return "skip"
    p = (src.parent / t) if not t.startswith("/") else (
        root / t.lstrip("/"))
    try:
        rel = p.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return str(rel) if (root / rel).is_file() else None


def normalized(text):
    """Note content with every inline-link target blanked — what the
    'don't touch the prose' hash is computed over."""
    return NORM_RE.sub(r"\1\2", text)


def generate():
    rng = random.Random(SEED)
    out = ROOT / "notes"
    tmp = ROOT / "notes.new"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()

    all_notes = dict(NOTES)
    for name, body in DAILY.items():
        # a small seeded tracker tail keeps the generator honest —
        # everything above is authored, this line is drawn
        all_notes[f"daily/{name}"] = body + (
            f"\n- log · mood {rng.randint(5, 9)}/10 · "
            f"steps {rng.randrange(6000, 14500, 100)} · "
            f"pages {rng.randint(0, 40)}\n")

    for rel, body in all_notes.items():
        path = tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    broken = []
    hashes = {}
    for rel, body in all_notes.items():
        hashes[f"notes/{rel}"] = hashlib.sha256(
            normalized(body).encode()).hexdigest()
        for i, line in enumerate(body.splitlines(), 1):
            for m in LINK_RE.finditer(line):
                t = link_target(m)
                if resolve(tmp, tmp / rel, t) is None:
                    broken.append({"file": f"notes/{rel}", "line": i,
                                   "target": t})

    # every broken link must be a *planned* break, with a known fix
    fix_keys = {(f"notes/{f}", t): c for (f, t), c in FIXES.items()}
    for b in broken:
        key = (b["file"], b["target"])
        assert key in fix_keys, f"unplanned broken link: {b}"
        b["correct_target"] = fix_keys[key]
        correct = resolve(tmp, tmp / b["file"].removeprefix("notes/"),
                          b["correct_target"])
        assert correct, f"correct target doesn't resolve: {key}"
        b["correct_resolved"] = f"notes/{correct}"
    assert len(broken) == len(FIXES), (
        f"expected {len(FIXES)} broken links, found {len(broken)}")

    folders = {}
    for rel in sorted(all_notes):
        folders.setdefault(rel.split("/", 1)[0], []).append(f"notes/{rel}")

    manifest = {
        "corpus": {
            "seed": SEED,
            "generator": "tools/gen_kb.py",
            "notes": len(all_notes),
            "folders": {k: len(v) for k, v in sorted(folders.items())},
        },
        "link_convention": (
            "relative markdown links [text](path.md) resolved against "
            "the linking note's folder; no wiki-links, no absolute "
            "paths; targets end in .md"),
        "notes_files": sorted(f"notes/{r}" for r in all_notes),
        "content_hashes": dict(sorted(hashes.items())),
        "broken_links": sorted(broken, key=lambda b: (b["file"],
                                                    b["line"])),
    }

    if out.exists():
        shutil.rmtree(out)
    tmp.rename(out)
    (ROOT / "tests" / "manifest.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True) + "\n")

    total = sum(f.stat().st_size for f in out.rglob("*.md"))
    print(f"wrote {len(all_notes)} notes, {total / 1024:.0f} KB total")
    for k, v in sorted(folders.items()):
        print(f"  {k}: {len(v)} notes")
    print(f"planted broken links: {len(broken)}")
    for b in broken:
        print(f"  {b['file']}:{b['line']}  {b['target']} -> "
              f"{b['correct_target']}")


if __name__ == "__main__":
    sys.exit(generate())
