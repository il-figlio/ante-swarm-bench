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
