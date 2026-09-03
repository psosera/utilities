#!/usr/bin/env python3
'''Check that every colour pair a book can render meets WCAG AA.

Colours arrive in layers: assets/css/book.css carries the built-in palette,
and each stylesheet named in the `additionalCss` field of book.yml is laid
over it. An
override may redefine the whole palette or only a few values, so what matters
is the *result* of stacking them, not any file on its own. This composes the
layers the same way the browser does, then checks the result.

    python3 assets/css/themes/check-contrast.py             # built-in, then
                                                            # each theme over it
    python3 assets/css/themes/check-contrast.py a.css b.css # one stack:
                                                            # book.css + a + b

Exits non-zero if any pair falls short, so it can gate a commit.
'''
import pathlib
import re
import sys

# (foreground token, background token, minimum ratio). 4.5 is AA for body text;
# 3.0 is AA for large text (>=24px, or >=18.7px bold) and for UI boundaries.
CHECKS = [
    ('fg', 'bg', 4.5), ('fg', 'bg-sidebar', 4.5), ('fg', 'bg-quote', 4.5),
    ('fg', 'bg-hover', 4.5), ('fg', 'bg-active', 4.5), ('fg-strong', 'bg', 4.5),
    ('fg-muted', 'bg', 4.5), ('fg-muted', 'bg-sidebar', 4.5),
    ('fg-muted', 'bg-quote', 4.5), ('fg-muted', 'bg-elevated', 4.5),
    ('fg-faint', 'bg-sidebar', 4.5), ('fg-faint', 'bg', 3.0),
    ('fg-heading', 'bg', 4.5), ('fg-heading', 'bg-sidebar', 4.5),
    ('fg-h1', 'bg', 4.5), ('fg-h2', 'bg', 4.5), ('fg-h3', 'bg', 4.5),
    ('link', 'bg', 4.5), ('link-hover', 'bg', 4.5), ('link-visited', 'bg', 4.5),
    ('link', 'bg-quote', 4.5),
    ('accent', 'bg', 3.0), ('accent', 'bg-sidebar', 3.0),
    ('accent', 'bg-active', 4.5),        # the current chapter, on its own fill
    ('accent-contrast', 'accent', 4.5),  # the skip link
    ('fg-code', 'bg-code', 4.5), ('fg-inline-code', 'bg-inline-code', 4.5),
    ('border-strong', 'bg-sidebar', 1.4),
] + [(f'syn-{token}', 'bg-code', 4.5) for token in (
    'comment', 'keyword', 'string', 'number', 'function', 'type', 'operator',
    'variable', 'error', 'warning')]

def luminance(colour: str) -> float:
    '''The relative luminance of a #rrggbb colour, per WCAG 2.'''
    digits = colour.lstrip('#')
    channels = [int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
              for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

def contrast(foreground: str, background: str) -> float:
    '''The WCAG contrast ratio between two #rrggbb colours.'''
    a, b = luminance(foreground), luminance(background)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)

def palette(text: str, prefix: str) -> dict[str, str]:
    '''Read the --l-* or --d-* values a theme stylesheet defines.'''
    return {name: value.strip() for name, value
            in re.findall(rf'--{prefix}-([\w-]+):\s*([^;]+);', text)}

def compose(paths: list[pathlib.Path], prefix: str) -> dict[str, str]:
    '''Stack the palettes in `paths`, later files winning, as CSS would.'''
    colours: dict[str, str] = {}
    for path in paths:
        colours |= palette(path.read_text(), prefix)
    return colours

def check(paths: list[pathlib.Path], label: str) -> bool:
    '''Report every pair in the stacked palette that falls short, in both modes.'''
    ok = True
    for mode, prefix in (('light', 'l'), ('dark', 'd')):
        colours = compose(paths, prefix)
        failures = []
        for foreground, background, needed in CHECKS:
            if foreground not in colours or background not in colours:
                failures.append(f'    missing token: '
                                f'--{prefix}-{foreground} or --{prefix}-{background}')
                continue
            pair = (colours[foreground], colours[background])
            if not all(value.startswith('#') for value in pair):
                continue          # rgba() shadows and scrims carry no text
            ratio = contrast(*pair)
            if ratio < needed:
                failures.append(f'    {foreground:18} on {background:15} '
                                f'{ratio:5.2f} : 1   (needs {needed})')
        print(f'  {label}/{mode}: {"FAIL" if failures else "ok"}')
        if failures:
            print('\n'.join(failures))
        ok &= not failures
    return ok

def main(argv: list[str]) -> int:
    '''Check the stack named in `argv`, or the built-in and each theme.'''
    here = pathlib.Path(__file__).resolve().parent
    base = here.parent / 'book.css'
    if not base.is_file():
        print(f'cannot find the built-in palette at {base}', file=sys.stderr)
        return 1
    if len(argv) > 1:
        overrides = [pathlib.Path(name) for name in argv[1:]]
        results = [check([base, *overrides],
                         ' + '.join(p.stem for p in overrides))]
    else:
        # The built-in palette alone, then each theme laid over it.
        results = [check([base], 'built-in')]
        results += [check([base, path], path.stem)
                    for path in sorted(here.glob('*.css'))]
    ok = all(results)
    print('\nall pairs meet WCAG AA' if ok else '\ncontrast failures above')
    return 0 if ok else 1

if __name__ == '__main__':
    sys.exit(main(sys.argv))
