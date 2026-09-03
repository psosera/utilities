# Colour schemes

Colours arrive in layers. `assets/css/book.css` carries a complete built-in
palette, and a book lays its own stylesheets over it by naming them in
`book.yml`:

```yaml
additionalCss: assets/css/themes/ocean.css
```

or, for more than one:

```yaml
additionalCss:
  - assets/css/themes/ocean.css
  - assets/branding/course.css
```

Paths are relative to the book root and must live under `assets/`, which is
copied into the build keeping its layout - so the path in `book.yml`, the path
on disk and the path in the published book are all the same string. An
absolute URL is passed through untouched. A local file outside `assets/` is
rejected at build time rather than left as a link to nothing. They load in order, after `book.css`, so later files win. A book
fixes its own scheme this way — there is no runtime switcher, and readers see
the scheme the book chose. The only thing a reader controls is light versus
dark, and every palette defines both.

Because the layers are plain custom properties, an override does **not** have
to be a whole palette. The files here redefine everything, but a stylesheet
that changes two values is equally valid:

```css
/* assets/branding/course.css — keep the scheme, change the heading colour. */
:root {
  --l-fg-h2: #7a2f8f;
  --d-fg-h2: #dda2ee;
}
```

That is the usual way to make one book look slightly different from another
without maintaining a second copy of a palette.

## What a palette file contains

Values only — `--l-*` for light mode, `--d-*` for dark. The mapping onto the
semantic tokens the rest of the stylesheet uses, and all the light/dark
switching, lives once in `assets/css/book.css`. A palette cannot get the
switching
wrong because it never expresses it.

The header comment is descriptive, not load-bearing:

```css
/*!
  name: Ocean
  swatch-light: #076c7d, #e9f2f5
  swatch-dark: #3fbdd6, #101f24
*/
```

Nothing reads it now that the palette menu is gone, but it is worth keeping so
a file says what it is. (An earlier version of the template built a runtime
menu from these fields.)

## Adding a scheme

1. Copy the file whose mood is closest to what you want.
2. Update the header comment, then edit the values.
3. Run the contrast checker (below).
4. Point `additionalCss` at it in `book.yml`.

## Readability

Every pair that carries text is checked against WCAG AA: 4.5:1 for body text,
3:1 for large text and interface boundaries. The checker composes the layers
the same way the browser does, so it measures what a reader actually sees:

```
python3 assets/css/themes/check-contrast.py                     # built-in
                                                                # alone, then
                                                                # each theme
python3 assets/css/themes/check-contrast.py ocean.css brand.css  # one stack
```

Pass the same files, in the same order, as `additionalCss` — a partial
override is only meaningful on top of what it overrides. It exits non-zero on
failure, so it can gate a commit.

Contrast is not the only thing that matters, but it is the thing that is easy
to get wrong while a palette still looks fine on the display you designed it
on. Check before shipping a scheme, not after a reader reports it.

## Printing

The dark rules in `assets/css/book.css` are scoped to `@media screen`, so a printed page
always uses the light half of whatever palette is in effect — a reader in dark
mode gets a white sheet, not a black one. If you add a scheme, its `--l-*`
values are what will come out of the printer.

## The token set

Light tokens are prefixed `--l-`, dark `--d-`. The built-in palette defines
every one; an override defines as many as it wants to change.

| Group | Tokens |
| ----- | ------ |
| Surfaces | `bg` `bg-elevated` `bg-sidebar` `bg-topbar` `bg-hover` `bg-active` `bg-code` `bg-inline-code` `bg-quote` |
| Text | `fg` `fg-strong` `fg-muted` `fg-faint` `fg-heading` `fg-h1` `fg-h2` `fg-h3` `fg-code` `fg-inline-code` |
| Accent and links | `accent` `accent-contrast` `link` `link-hover` `link-visited` |
| Lines and overlays | `border` `border-strong` `border-code` `shadow-md` `scrim` |
| Syntax | `syn-comment` `syn-keyword` `syn-string` `syn-number` `syn-function` `syn-type` `syn-operator` `syn-variable` `syn-error` `syn-warning` |

Notes on the ones that are easy to misread:

- `bg-topbar` is drawn over scrolling content behind a blur, so it usually
  wants to be the page background at around 85% alpha rather than opaque.
- `accent` sits on `bg-active` as the current chapter's label, so those two
  need 4.5:1 between them, not just 3:1 against the page.
- `fg-faint` is the colour of a chapter marked `visible: false` in the
  sidebar. It should read as de-emphasised but still be legible.
- `fg-h1`, `fg-h2` and `fg-h3` may differ from `fg-heading`; that is where a
  scheme gets to put colour into the page without touching body text.
- `shadow-md` and `scrim` are full CSS values (`rgba(...)`, a shadow list),
  not bare hex, and the contrast checker skips them.
