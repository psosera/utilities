#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [ "pyyaml" ]
# ///
'''Build a book of teaching materials from Markdown sources using pandoc.'''

from argparse import ArgumentParser
from collections.abc import Iterator
from dataclasses import dataclass, field
from html import escape
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, BinaryIO, ClassVar, override
from urllib.parse import urlparse
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

import yaml

# A decoded fragment of pandoc's JSON AST, whose shape depends on its 't' tag.
type Json = Any

# Everything under this directory is copied into the build unchanged, keeping
# its full path: assets/css/book.css becomes book/assets/css/book.css. Unlike
# src/, whose name is stripped because its contents become the pages, this
# directory keeps its own - which is what stops a chapter's own image at
# src/img/logo.svg from silently overwriting a theme's assets/img/logo.svg.
# It lives outside this script so a book's look can be updated without it.
ASSETS_DIR = 'assets'

# One past h6: a level no heading can reach, so nothing is folded by default.
UNFOLDED = 7

class BookError(Exception):
    '''A problem with a book project, reported without a traceback.'''

##### Configuration Reading ####################################################

@dataclass
class Config:
    '''The contents of a book project's book.yml file.'''
    title: str
    author: str
    footer: str = ''
    useKatex: bool = False
    extraPandocFlags: str = ''
    # Stylesheets layered over the built-in theme: a path from the book root
    # (which must be under assets/), an absolute URL, or a list of either. A book fixes its own colour scheme
    # here; readers cannot change it.
    additionalCss: str | list[str] = ()
    # Headings at this level or deeper start folded in the table of contents,
    # so a long outline opens showing its shape rather than all its leaves.
    # 2 folds h2 and everything below it; 7 is past h6, so nothing folds.
    defaultFoldedLevel: int = UNFOLDED

    @property
    def stylesheets(self) -> list[str]:
        '''The extra stylesheets, however `additionalCss` was written.'''
        if isinstance(self.additionalCss, str):
            return [self.additionalCss] if self.additionalCss else []
        return [str(sheet) for sheet in self.additionalCss or ()]

def read_configuration(path: Path) -> Config:
    '''Read the book configuration recorded at `path`.'''
    return Config(**yaml.safe_load(path.read_text()))

def is_external(sheet: str) -> bool:
    '''Whether `sheet` names somewhere other than a file in the book.'''
    return bool(urlparse(sheet).scheme) or sheet.startswith('/')

################################################################################

##### SUMMARY.md File Reading ##################################################

# Where an entry leads: a chapter under src/, an external URL, or nowhere.
type Destination = Path | str | None

@dataclass
class TocNode:
    '''One node of the table of contents.

    A node is either a heading from SUMMARY.md or an entry in a bulleted list
    beneath one. `level` is the Markdown heading level that introduced it, or
    0 for a list entry. Headings carry the book's structure - a book might use
    `#` for parts, a course `#`/`##`/`###` for parts, weeks and days - and the
    entries beneath them point at the chapters themselves.
    '''
    title: str
    # The same name twice: plain text for storage keys and labels, and HTML
    # for display, so that emphasis written in SUMMARY.md survives into the
    # outline.
    html: str = ''
    # What the page calls itself, in the same two forms, taken from the
    # chapter's own front matter. A schedule names an entry by its date, so
    # this is kept apart from the name above: the outline reads
    # `W 09/09: Needfinding`, the page it opens `Needfinding and ideation`.
    page_title: str = ''
    page_html: str = ''
    destination: Destination = None
    level: int = 0
    visible: bool = True
    children: list['TocNode'] = field(default_factory=list)

    @property
    def is_section(self) -> bool:
        '''Whether this node came from a heading rather than a list item.'''
        return self.level > 0

    @property
    def is_link(self) -> bool:
        '''Whether this node renders as a link rather than as plain text.'''
        return isinstance(self.destination, str) or (
            isinstance(self.destination, Path) and self.visible)

type Nodes = list[TocNode]
type Toc = Nodes

# A YAML metadata block: three dashes, the metadata, then dashes or dots.
FRONT_MATTER = re.compile(r'\A---[ \t]*\r?\n(.*?)\r?\n(?:---|\.\.\.)[ \t]*$',
                          re.S | re.M)

def pandoc(*args: str | Path, stdin: str = '') -> str:
    '''Run pandoc on `args`, returning what it writes to standard output.

    `stdin` is the document to read when no file is named among `args`;
    pandoc ignores it otherwise.
    '''
    argv = ['pandoc', *(str(arg) for arg in args)]
    return subprocess.run(argv, check=True, capture_output=True, text=True,
                          input=stdin).stdout

def find_links(node: Json) -> list[Json]:
    '''Collect every Link node within `node`, in document order.'''
    match node:
        case list():
            return [link for child in node for link in find_links(child)]
        case {'t': 'Link'}:
            return [node]           # links do not nest, so do not recurse
        case {'c': content}:
            return find_links(content)
        case _:
            return []

def inline_text(node: Json) -> str:
    '''Flatten an inline node, or a list of them, into plain text.'''
    match node:
        case list():
            return ''.join(inline_text(child) for child in node)
        case {'t': 'Str', 'c': str(text)}:
            return text
        case {'t': 'Space' | 'SoftBreak' | 'LineBreak'}:
            return ' '
        case {'t': 'Code' | 'Math', 'c': [_, str(text)]}:
            return text
        case {'c': content}:
            return inline_text(content)
        case _:
            return ''

def inline_html(node: Json) -> str:
    """Render an inline node, or a list of them, as HTML.

    SUMMARY.md is Markdown, so an entry may be emphasised, bold, struck
    through or hold a snippet of code, and that formatting belongs in the
    outline as much as in the prose. A link renders as its text alone: the
    outline supplies its own links, and an <a> nested inside one - or inside
    the button of a collapsible row - would not be valid HTML.
    """
    match node:
        case list():
            return ''.join(inline_html(child) for child in node)
        case {'t': 'Str', 'c': str(text)}:
            return escape(text)
        case {'t': 'Space' | 'SoftBreak'}:
            return ' '
        case {'t': 'LineBreak'}:
            return '<br>'
        case {'t': 'Emph', 'c': content}:
            return f'<em>{inline_html(content)}</em>'
        case {'t': 'Strong', 'c': content}:
            return f'<strong>{inline_html(content)}</strong>'
        case {'t': 'Underline', 'c': content}:
            return f'<u>{inline_html(content)}</u>'
        case {'t': 'Strikeout', 'c': content}:
            return f'<del>{inline_html(content)}</del>'
        case {'t': 'Superscript', 'c': content}:
            return f'<sup>{inline_html(content)}</sup>'
        case {'t': 'Subscript', 'c': content}:
            return f'<sub>{inline_html(content)}</sub>'
        case {'t': 'SmallCaps', 'c': content}:
            return f'<span class="smallcaps">{inline_html(content)}</span>'
        case {'t': 'Code', 'c': [_, str(text)]}:
            return f'<code>{escape(text)}</code>'
        case {'t': 'Math', 'c': [_, str(text)]}:
            return escape(text)
        case {'t': 'Quoted', 'c': [{'t': str(kind)}, content]}:
            open_mark, close_mark = ('\u2018', '\u2019') if kind == 'SingleQuote' \
                else ('\u201c', '\u201d')
            return f'{open_mark}{inline_html(content)}{close_mark}'
        case {'t': 'RawInline', 'c': [str(fmt), str(text)]}:
            return text if fmt.startswith('html') else ''
        case {'t': 'Link' | 'Image' | 'Cite', 'c': [_, content, *_]}:
            return inline_html(content)
        case {'t': 'Span', 'c': [_, content]}:
            return inline_html(content)
        case {'t': 'Note'}:
            return ''
        case {'c': content}:
            return inline_html(content)
        case _:
            return ''

def list_items(block: Json) -> list[Json]:
    '''Read the items of `block`, if it is a bulleted or numbered list.'''
    match block:
        case {'t': 'BulletList', 'c': items}:
            return list(items)
        case {'t': 'OrderedList', 'c': [_, items]}:
            return list(items)
        case _:
            return []

def read_entry(blocks: list[Json]) -> TocNode:
    '''Read the `blocks` of one list item as an entry and its children.'''
    text: Json = next((b['c'] for b in blocks if b['t'] in ('Plain', 'Para')), [])
    links = find_links(text)
    # A Link's contents are [attributes, the title, [destination, tooltip]].
    _, title, (destination, _) = links[0]['c'] if links else (None, text, ('', ''))
    return TocNode(
        title=inline_text(title),
        html=inline_html(title),
        destination=read_destination(destination) if links else None,
        children=[read_entry(item) for block in blocks
                  for item in list_items(block)])

def read_destination(destination: str) -> Destination:
    '''Read a link as an external URL, or as a chapter source under src/.'''
    return destination if urlparse(destination).scheme else Path(destination)

def read_summary_file(path: Path) -> Toc:
    '''Read the book's table of contents from the outline in `path`.

    Headings nest by level, and the bulleted lists beneath a heading become
    that heading's entries. A heading closes every open heading at its own
    level or deeper, exactly as a reader would expect from the indentation.
    '''
    root = TocNode(title='')
    open_sections = [root]
    for block in json.loads(pandoc('-t', 'json', path))['blocks']:
        match block:
            case {'t': 'Header', 'c': [int(level), _, title]}:
                while len(open_sections) > 1 and open_sections[-1].level >= level:
                    open_sections.pop()
                section = TocNode(title=inline_text(title),
                                  html=inline_html(title), level=level)
                open_sections[-1].children.append(section)
                open_sections.append(section)
            case _:
                open_sections[-1].children += [
                    read_entry(item) for item in list_items(block)]
    return root.children

def chapters(nodes: Nodes) -> Iterator[tuple[Path, TocNode]]:
    '''Every chapter source at or beneath `nodes`, and its entry, in order.'''
    for node in nodes:
        if isinstance(node.destination, Path):
            yield node.destination, node
        yield from chapters(node.children)

def read_front_matter(path: Path) -> dict[str, Any]:
    '''Read the YAML metadata block at the top of `path`, if it has one.'''
    if not path.is_file():
        return {}
    match = FRONT_MATTER.match(path.read_text(encoding='utf-8'))
    if match is None:
        return {}
    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return metadata if isinstance(metadata, dict) else {}

def read_titles(titles: list[Any]) -> list[tuple[str, str] | None]:
    '''Read the front matter titles in `titles` as plain text and as HTML.

    A title is Markdown, as a name in SUMMARY.md is, so `*Prototyping*` opens
    its page as emphasis rather than as asterisks. They are read together, as
    one metadata block, rather than a pandoc run apiece: a course may run to a
    hundred chapters, and a preview rebuilds all of them on every save. A
    title that is missing, blank, or not text at all reads as None, leaving
    the entry the name the outline gave it.
    '''
    keys = [f'title{i}' if isinstance(title, str) and title.strip() else None
            for i, title in enumerate(titles)]
    named = {key: title for key, title in zip(keys, titles) if key is not None}
    if not named:
        return [None] * len(titles)
    block = yaml.safe_dump(named, allow_unicode=True, sort_keys=False)
    meta = json.loads(pandoc('-t', 'json', '-f', 'markdown',
                             stdin=f'---\n{block}---\n'))['meta']
    return [(inline_text(value), inline_html(value)) if value else None
            for value in (meta.get(key) if key else None for key in keys)]

def resolve_pages(nodes: Nodes, src_dir: Path) -> None:
    '''Settle what each entry takes from the chapter it points at.

    A chapter's front matter decides two things about its entry: whether the
    entry links to it at all - `visible: false` keeps the entry in place, so a
    schedule still reads correctly, but renders it as plain text rather than
    as a link - and what the page it opens calls itself. A chapter that gives
    itself no title, and an entry that names no chapter, keep the name
    SUMMARY.md gave them.
    '''
    entries = list(chapters(nodes))
    metadata = [read_front_matter(src_dir / chapter) for chapter, _ in entries]
    titles = read_titles([data.get('title') for data in metadata])
    for (_, node), data, title in zip(entries, metadata, titles):
        node.visible = data.get('visible', True) is not False
        node.page_title, node.page_html = title or (node.title, node.html)

################################################################################

##### Content Generation #######################################################

@dataclass
class Book:
    '''A book project: its root directory, configuration, and contents.'''
    root: Path
    config: Config
    toc: Toc

    @property
    def src_dir(self) -> Path:
        '''The directory holding the book's Markdown sources.'''
        return self.root / 'src'

    @property
    def book_dir(self) -> Path:
        '''The directory holding the book's build output.'''
        return self.root / 'book'

    @property
    def assets_dir(self) -> Path:
        '''The directory holding files copied into the build unchanged.'''
        return self.root / ASSETS_DIR

    def under_assets(self, sheet: str) -> bool:
        '''Whether `sheet` names a file inside the assets directory.'''
        try:
            return (self.root / sheet).resolve().is_relative_to(
                self.assets_dir.resolve())
        except OSError:
            return False

    @property
    def template(self) -> Path:
        '''The pandoc template each chapter is rendered with.'''
        return self.root / 'template.html'

    @property
    def inputs(self) -> list[Path]:
        '''Every file a build reads, so a preview knows what to watch.'''
        return [self.root / 'book.yml', self.template,
                *(p for p in self.src_dir.rglob('*') if p.is_file()),
                *(p for p in self.assets_dir.rglob('*') if p.is_file())]

    @property
    def chapters(self) -> list[tuple[Path, TocNode]]:
        '''Every chapter named anywhere in the table of contents, and its entry.'''
        return list(chapters(self.toc))

    @property
    def index(self) -> Path | None:
        '''The chapter a reader lands on from the book's title.'''
        return next((chapter for chapter, _ in self.chapters), None)

    def page(self, chapter: Path) -> Path:
        '''The HTML file built from the chapter source at `chapter`.'''
        return self.book_dir / chapter.with_suffix('.html')

    def link(self, chapter: Path, page: Path) -> str:
        '''Link to `chapter` as a URL relative to the page at `page`.'''
        return os.path.relpath(self.page(chapter), page.parent).replace(os.sep, '/')

    def root_prefix(self, page: Path) -> str:
        '''The path back to the book's root from the page at `page`.

        Chapters may sit in subdirectories, so stylesheets and scripts are
        linked through this rather than from an absolute path - which keeps a
        built book working when it is served from a subdirectory.
        '''
        relative = os.path.relpath(self.book_dir, page.parent).replace(os.sep, '/')
        return '' if relative == '.' else f'{relative}/'

def is_current_page(node: TocNode, book: 'Book', page: Path) -> bool:
    '''Whether `node` points at the page currently being rendered.'''
    return (isinstance(node.destination, Path) and node.visible
            and book.page(node.destination) == page)

# Drawn once in the template, referenced by every toggle, rather than repeating
# the path data for each of what may be a hundred rows.
CHEVRON = ('<span class="toc-chevron">'
           '<svg aria-hidden="true"><use href="#i-chevron"/></svg></span>')

def render_label(node: TocNode, book: Book, page: Path) -> str:
    '''Render a node's title, as a link when it leads somewhere visible.'''
    title = node.html
    match node.destination:
        case Path() as chapter if node.visible:
            href = escape(book.link(chapter, page), quote=True)
            current = ' aria-current="page"' if book.page(chapter) == page else ''
            return f'<a class="toc-link" href="{href}"{current}>{title}</a>'
        case str() as url:
            return f'<a class="toc-link" href="{escape(url, quote=True)}">{title}</a>'
        case _:
            return f'<span class="toc-text">{title}</span>'

def render_node(node: TocNode, book: Book, page: Path, key: str) -> str:
    '''Render one node: its own row, then its children as a nested list.'''
    classes = ['toc-item']
    if node.is_section:
        classes.append('toc-item--section')
    if not node.visible:
        classes.append('toc-item--hidden')
    attributes = f' class="{' '.join(classes)}"'
    if node.is_section:
        attributes += f' data-level="{node.level}"'

    children = render_nodes(node.children, book, page, key)
    label = render_label(node, book, page)
    row_classes = ['toc-row']
    # A row acts if it goes somewhere or folds something. One that does
    # neither - a plain bullet with no link beneath it - is just a label, and
    # must not light up under the pointer as though it could be clicked.
    if node.is_link or node.children:
        row_classes.append('toc-row--clickable')
    if is_current_page(node, book, page):
        row_classes.append('toc-row--current')

    row = label
    if children:
        # Keyed by title path rather than position, so that inserting a
        # section elsewhere does not shuffle every reader's saved state.
        attributes += f' data-toc-key="{escape(key, quote=True)}"'
        # Only headings fold by default; a bulleted entry has no level to
        # compare, and its children are the readings themselves.
        folded = node.is_section and node.level >= book.config.defaultFoldedLevel
        if folded:
            attributes += ' data-collapsed'
        # A row that leads nowhere makes its whole width the toggle, so the
        # target is the row rather than a chevron a few pixels wide. A row
        # that is also a link cannot: the label has to stay a link, so only
        # the chevron toggles and the label navigates.
        span = '' if node.is_link else ' toc-toggle--row'
        toggle = (f'<button class="toc-toggle{span}" type="button"'
                  f' aria-expanded="{'false' if folded else 'true'}"'
                  f' aria-label="Toggle {escape(node.title, quote=True)}">'
                  f'{CHEVRON}')
        row = f'{toggle}</button>{label}' if node.is_link else f'{toggle}{label}</button>'

    return (f'<li{attributes}><div class="{' '.join(row_classes)}">{row}</div>'
            f'{children}</li>')

def render_nodes(nodes: Nodes, book: Book, page: Path, prefix: str = '') -> str:
    '''Render `nodes` and their descendants as a nested, collapsible list.'''
    if not nodes:
        return ''
    items = [render_node(node, book, page,
                         f'{prefix}/{node.title}' if prefix else node.title)
             for node in nodes]
    return f'<ol class="toc-list">{''.join(items)}</ol>'

def render_additional_css(book: Book, page: Path) -> str:
    '''Render <link> tags for the book's extra stylesheets, in order.

    These load after the built-in theme, so a stylesheet listed here need only
    redefine the palette values it wants to change.
    '''
    prefix = book.root_prefix(page)
    return '\n'.join(
        f'<link rel="stylesheet" href="'
        f'{escape(sheet if is_external(sheet) else prefix + sheet, quote=True)}">'
        for sheet in book.config.stylesheets)

def render_toc(book: Book, page: Path) -> str:
    '''Render the whole table of contents, with links relative to `page`.'''
    return f'<div class="toc">{render_nodes(book.toc, book, page)}</div>'

def read_book(root: Path) -> Book:
    '''Read the book project rooted at `root`.'''
    config = read_configuration(root / 'book.yml')
    toc = read_summary_file(root / 'src' / 'SUMMARY.md')
    resolve_pages(toc, root / 'src')
    level = config.defaultFoldedLevel
    # bool is an int in Python, and `defaultFoldedLevel: yes` reads as True,
    # which would silently fold every heading. Reject it with the rest.
    if isinstance(level, bool) or not isinstance(level, int) \
            or not 1 <= level <= UNFOLDED:
        raise BookError(f'defaultFoldedLevel must be a whole number from 1 to '
                        f'{UNFOLDED}, not {level!r}')
    book = Book(root, config, toc)
    # A stylesheet is named by its path from the book root, which is also its
    # path in the build. Only assets/ is copied, so a file anywhere else would
    # leave a link pointing at nothing; say so rather than shipping that.
    for sheet in config.stylesheets:
        if is_external(sheet):
            continue
        if not (root / sheet).is_file():
            raise BookError(f'additionalCss lists {sheet}, '
                            f'but {root / sheet} does not exist')
        if not book.under_assets(sheet):
            raise BookError(f'additionalCss lists {sheet}, which is outside '
                            f'{ASSETS_DIR}/; only files under {ASSETS_DIR}/ are '
                            f'copied into the build')
    return book

def find_root() -> Path:
    '''Find the book project enclosing the current directory.'''
    cwd = Path.cwd()
    return next(p for p in (cwd, *cwd.parents) if (p / 'book.yml').is_file())

def convert(src_path: Path, dst_path: Path, entry: TocNode, book: Book) -> None:
    '''Render the chapter `entry` from `src_path` to a page at `dst_path`.'''
    cfg = book.config
    # Metadata is parsed as Markdown, which is what prose like the title wants.
    # Everything pre-rendered goes through -V, which pandoc passes through
    # untouched - no smart quotes mangling markup or hex colours.
    # Deliberately not 'title'/'author': a chapter's own YAML front matter
    # outranks --metadata-file, so a page whose front matter says
    # `title: Syllabus` would otherwise replace the book's title in the top
    # bar. These names cannot collide with anything a chapter would set -
    # and that front matter title has already been read into the entry, so it
    # is what names the page below.
    metadata = {'book-title': cfg.title, 'book-author': cfg.author,
                'book-footer': cfg.footer, 'chapter-title': entry.page_title}
    index = book.index
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', suffix='.yml', delete_on_close=False) as f:
        yaml.safe_dump(metadata, f, allow_unicode=True, sort_keys=False)
        f.close()
        pandoc('--standalone', f'--template={book.template}',
               f'--metadata-file={f.name}',
               '-V', f'toc={render_toc(book, dst_path)}',
               '-V', f'root={book.root_prefix(dst_path)}',
               '-V', f'index={book.link(index, dst_path) if index else ""}',
               '-V', f'additional-css={render_additional_css(book, dst_path)}',
               # The heading opening the page, with the emphasis its own
               # title was written with. <title> keeps the plain form above:
               # markup in a title element is text, not markup.
               '-V', f'chapter-title-html={entry.page_html}',
               *(['--katex'] if cfg.useKatex else []),
               *shlex.split(cfg.extraPandocFlags), src_path, '-o', dst_path)

# Documentation and tooling live beside the assets they describe, but have no
# business being published with the book.
NOT_ASSETS = shutil.ignore_patterns('*.py', '*.md', '.*', '__pycache__')

def copy_assets(book: Book) -> None:
    '''Copy everything under assets/ into the build, keeping its layout.

    The tree is reproduced under book/assets/, so a path in book.yml or in the
    template reads the same as the path on disk, and the pages a reader
    browses never share a directory with the machinery that styles them.
    '''
    if book.assets_dir.is_dir():
        shutil.copytree(book.assets_dir, book.book_dir / ASSETS_DIR,
                        dirs_exist_ok=True, ignore=NOT_ASSETS)

def build(book: Book) -> None:
    '''Render every chapter, then copy the assets and remaining sources.'''
    for chapter, entry in book.chapters:
        source = book.src_dir / chapter
        if not source.is_file():
            raise BookError(f'{source} is listed in SUMMARY.md but does not exist')
        convert(source, book.page(chapter), entry, book)
    copy_assets(book)
    for asset in book.src_dir.rglob('*'):
        if asset.is_file() and asset.suffix != '.md':
            destination = book.book_dir / asset.relative_to(book.src_dir)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(asset, destination)

################################################################################

##### Preview Server ###########################################################

VERSION_URL = '/pmbook-version'

RELOAD_SCRIPT = f'''<script>
setInterval(async () => {{
  const version = await (await fetch('{VERSION_URL}')).text();
  window.pmbookVersion ??= version;
  if (window.pmbookVersion !== version) location.reload();
}}, 500);
</script>'''.encode()

class PreviewHandler(SimpleHTTPRequestHandler):
    '''Serve a built book, reloading the browser after every rebuild.'''

    # Bumped on each rebuild; pages poll VERSION_URL to notice the change.
    version: ClassVar[int] = 0

    def respond(self, content_type: str, body: bytes) -> io.BytesIO:
        '''Send the headers for `body`, and return it to be written out.'''
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        return io.BytesIO(body)

    @override
    def send_head(self) -> io.BytesIO | BinaryIO | None:
        '''Serve the version of the current build, and pages that poll it.'''
        if self.path == VERSION_URL:
            return self.respond('text/plain', str(self.version).encode())
        target = Path(self.translate_path(self.path))
        target = target / 'index.html' if self.path.endswith('/') else target
        if target.suffix != '.html' or not target.is_file():
            return super().send_head()
        body = target.read_bytes().replace(b'</body>', RELOAD_SCRIPT + b'</body>')
        return self.respond('text/html; charset=utf-8', body)

    @override
    def end_headers(self) -> None:
        '''Forbid caching of everything served.

        A preview exists to show the current build. Without this a browser is
        free to keep serving a stylesheet or script it fetched before the last
        edit, which reads as the edit having had no effect.
        '''
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    @override
    def log_message(self, format: str, *args: Any) -> None:
        '''Serve quietly, so that only rebuilds are reported.'''

def serve(book: Book, host: str, port: int) -> None:
    '''Serve the built book, rebuilding it whenever a source changes.'''
    handler = partial(PreviewHandler, directory=str(book.book_dir))
    Thread(target=ThreadingHTTPServer((host, port), handler).serve_forever,
           daemon=True).start()
    print(f'Serving {book.config.title} on http://{host}:{port}/ (Ctrl-C to stop)')
    modified: dict[Path, int] = {}
    while True:
        current = {path: path.stat().st_mtime_ns for path in book.inputs}
        if current != modified:
            modified = current
            try:
                book = read_book(book.root)
                build(book)
            except BookError as error:
                print(f'error: {error}', file=sys.stderr, flush=True)
            else:
                PreviewHandler.version += 1
                print(f'Rebuilt at {time.strftime('%H:%M:%S')}', flush=True)
        time.sleep(0.3)

################################################################################

##### Commands #################################################################

# The files a new book starts from - template.html and an assets/ tree - live
# beside this script rather than inside it, so that a book's look can be
# updated independently of the builder.
SCAFFOLD = Path(__file__).resolve().parent / 'pmbook-template'

BOOK_YML = '''\
title: {title}
author: {author}
footer: ''
useKatex: false
extraPandocFlags: ''
additionalCss: []
defaultFoldedLevel: 7
'''

SUMMARY_MD = '''\
+ [Introduction](introduction.md)
'''

INTRODUCTION_MD = '''\
# Introduction

Welcome to *{title}*!
'''

def exec_init(title: str, path: Path) -> None:
    '''Create a new book project named `title` at `path`.'''
    if not SCAFFOLD.is_dir():
        raise BookError(f'{SCAFFOLD} is missing; it holds the default template '
                        f'and assets a new book starts from')
    (path / 'src').mkdir(parents=True)
    author = os.environ.get('USER', 'Anonymous')
    (path / 'book.yml').write_text(BOOK_YML.format(title=title, author=author))
    shutil.copytree(SCAFFOLD, path, dirs_exist_ok=True)
    (path / 'src' / 'SUMMARY.md').write_text(SUMMARY_MD)
    (path / 'src' / 'introduction.md').write_text(INTRODUCTION_MD.format(title=title))
    print(f'Created {title} in {path}')

def exec_build() -> None:
    '''Build the book project enclosing the current directory.'''
    book = read_book(find_root())
    build(book)
    print(f'Built {len(book.chapters)} chapter(s) into {book.book_dir}')

def exec_clean() -> None:
    '''Delete the build output of the project enclosing this directory.'''
    shutil.rmtree(find_root() / 'book', ignore_errors=True)

def exec_preview(host: str, port: int) -> None:
    '''Serve the project enclosing this directory, rebuilding as it changes.'''
    try:
        serve(read_book(find_root()), host, port)
    except KeyboardInterrupt:
        print('\nStopped.')

################################################################################

def main(args: list[str]) -> None:
    '''Run the sub-command named by the command line in `args`.'''
    parser = ArgumentParser(prog='pmbook', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='create a new book project')
    init.add_argument('title', help='the title of the book')
    init.add_argument('path', type=Path, help='where to create the project')
    commands.add_parser('build', help='build the book project in this directory')
    commands.add_parser('clean', help='delete the build output of this project')
    preview = commands.add_parser('preview', help='serve the book as it changes')
    preview.add_argument('--host', default='127.0.0.1')
    preview.add_argument('--port', type=int, default=8000)
    parsed = parser.parse_args(args[1:])
    try:
        match parsed.command:
            case 'init': exec_init(parsed.title, parsed.path)
            case 'build': exec_build()
            case 'clean': exec_clean()
            case 'preview': exec_preview(parsed.host, parsed.port)
    except BookError as error:
        sys.exit(f'error: {error}')

if __name__ == '__main__':
    main(sys.argv)
