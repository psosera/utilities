#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6.0"]
# ///
"""Build a static website from Markdown files with Pandoc.

A website project is any directory tree whose root holds a `config.yml` of site
metadata. Building mirrors that tree into `_build`: Markdown files are run
through Pandoc to become HTML, everything else is copied verbatim, and the
site's own control files are left behind.

A page is built into a directory of its own, so `about.md` becomes
`about/index.html` and is served as `/about/`. An `index.md` is already the
page for its directory and keeps its place.

Because that moves a page away from the depth of its source file, the relative
URLs written in a page's Markdown are rewritten to absolute ones: a link to
`lab1.md` becomes a link to `lab1/`, and a link to a copied file such as
`handout.pdf` keeps pointing at the copy. A link to something that the build
does not contain is reported. Templates are left to link wherever they please.

While `serve` is running it watches the site's files and builds them again as
they change, and every page it sends carries a small script that reloads that
page once a rebuild lands. The script is added to a page as it goes out, so it
is never part of the built site itself.

Every field in `config.yml` is passed to Pandoc as a template variable, so a
template can write `$siteName$` or any other key. Three fields mean something
to the script itself: `siteName`, which names the site, `baseUrl`, which every
rewritten URL is rooted at, and `pandoc`, an optional list of further options
to pass to Pandoc.

`title` is left to the pages: a page names itself in a YAML block of its own,
and the template writes that name as the page's heading. A page's fields are
read after the site's, so a page can also give any other field a value of its
own.

Usage:
    ./simple-website.py init  [PATH]
    ./simple-website.py build [PATH] [--clean] [--base-url URL]
    ./simple-website.py serve [PATH] [--port PORT] [--no-reload]
    ./simple-website.py clean [PATH]
"""

import argparse
import functools
import io
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http import HTTPStatus
from http.server import (
    BaseHTTPRequestHandler,
    SimpleHTTPRequestHandler,
    ThreadingHTTPServer,
)
from pathlib import Path
from typing import IO, Any

import yaml

#: Marks the root of a website project; holds the site metadata.
CONFIG_NAME = 'config.yml'

#: The Pandoc HTML template, looked up from a page's directory upward.
TEMPLATE_NAME = 'template.pandoc.html'

#: Where the built site is written, relative to the site root.
BUILD_NAME = '_build'

#: Files that drive the build and so are never copied into it.
SPECIAL_NAMES = frozenset({CONFIG_NAME, TEMPLATE_NAME})

#: Extensions held to be Markdown pages, matched without regard to case.
MARKDOWN_SUFFIXES = frozenset({'.md', '.markdown'})

#: Extensions held to be pages `serve` adds its reload script to.
HTML_SUFFIXES = frozenset({'.html', '.htm'})

#: The files that stand for a directory, in the order they are looked for.
INDEX_NAMES = ('index.html', 'index.htm')

#: Where `serve` listens unless told otherwise.
DEFAULT_PORT = 8000

#: Where `serve` announces its rebuilds, and the served pages listen for them.
RELOAD_PATH = '/_reload'

#: How long `serve` waits between looks over the site's files, in seconds.
POLL_SECONDS = 0.5

#: How long a quiet announcement stream waits before writing anyway, in
#: seconds, which is how a page that has gone away is noticed.
HEARTBEAT_SECONDS = 15.0

#: The end of the body of a page, where the reload script is added.
BODY_END = re.compile(rb'</body\s*>', re.IGNORECASE)

#: Added to every page `serve` sends: it listens at `RELOAD_PATH`, which the
#: `%s` is filled in with, and reloads the page when a rebuild is announced
#: there. An `EventSource` reconnects on its own, so the page lives through a
#: restart of the server.
RELOAD_SCRIPT = """\
<script>
/* Added by simple-website.py serve; not part of the built site. */
(function () {
  if (typeof EventSource !== 'function') {
    return;
  }
  var source = new EventSource('%s');
  source.addEventListener('reload', function () {
    window.location.reload();
  });
})();
</script>
"""

#: A Pandoc filter rewriting the URLs a page's Markdown links to.
LINK_FILTER = r"""
-- Rewrite the relative URLs in a page's content to suit the built site.
--
-- A Markdown target becomes the directory its page is built into, so that
-- `labs/lab1.md` points at `labs/lab1/`. Every other relative target is
-- resolved against the page's own directory, which keeps a link to a copied
-- file pointing at the copy. Both come out absolute, rooted at the site's
-- baseUrl, because a page is no longer served at the depth of its source
-- file. A URL that is already absolute, rooted at the server, or nothing but
-- a fragment is left alone.
--
-- Every URL this rewrites is appended to the file named by `linkLog`, so that
-- the build can afterwards check that each one landed somewhere real.

local MARKDOWN = { md = true, markdown = true }

local function segments(path)
  local parts = {}
  for segment in path:gmatch("[^/]+") do
    if segment == ".." then
      if #parts > 0 then
        table.remove(parts)
      end
    elseif segment ~= "." then
      parts[#parts + 1] = segment
    end
  end
  return parts
end

local function fixed(target)
  return target == ""
    or target:find("^#") ~= nil
    or target:find("^/") ~= nil
    or target:find("^%a[%w+.-]*:") ~= nil
end

local function page_stem(name)
  local stem, extension = name:match("^(.*)%.([^.]+)$")
  if stem ~= nil and MARKDOWN[extension:lower()] then
    return stem
  end
  return nil
end

local function resolve(base, dir, target)
  local path = target:match("^[^#]*")
  local fragment = target:sub(#path + 1)
  local parts = segments(dir .. "/" .. path)
  if #parts == 0 then
    return base .. fragment
  end
  local stem = page_stem(parts[#parts])
  if stem == nil then
    return base .. table.concat(parts, "/") .. fragment
  end
  if stem:lower() == "index" then
    table.remove(parts)
  else
    parts[#parts] = stem
  end
  local page = table.concat(parts, "/")
  if page ~= "" then
    page = page .. "/"
  end
  return base .. page .. fragment
end

function Pandoc(doc)
  local base = pandoc.utils.stringify(doc.meta.baseUrl or "")
  local dir = pandoc.utils.stringify(doc.meta.pageDir or "")
  local page = pandoc.utils.stringify(doc.meta.pageSource or "")
  local logged = {}

  local function rewrite(target)
    if fixed(target) then
      return target
    end
    local url = resolve(base, dir, target)
    logged[#logged + 1] = page .. "\t" .. target .. "\t" .. url
    return url
  end

  local result = doc:walk {
    Link = function(element)
      element.target = rewrite(element.target)
      return element
    end,
    Image = function(element)
      element.src = rewrite(element.src)
      return element
    end,
  }

  local log = pandoc.utils.stringify(doc.meta.linkLog or "")
  if log ~= "" and #logged > 0 then
    local handle = io.open(log, "a")
    if handle ~= nil then
      handle:write(table.concat(logged, "\n"), "\n")
      handle:close()
    end
  end
  return result
end
"""

STARTER_CONFIG = """\
siteName: My Site
baseUrl: https://example.com/
"""

STARTER_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$if(title)$$title$ · $endif$$siteName$</title>
</head>
<body>
<header><a href="$baseUrl$">$siteName$</a></header>
<main>
$if(title)$
<h1>$title$</h1>
$endif$
$body$
</main>
</body>
</html>
"""

STARTER_PAGE = """\
---
title: Welcome
---

Edit `index.md` to change this page. A page names itself in the block above,
and the template writes that name as the heading, so the text here starts at
a second-level heading.
"""


class SiteError(Exception):
    """A problem with the site: no config, bad config, or a failed Pandoc run."""


@dataclass(frozen=True)
class Site:
    """A website project: its root directory and its metadata."""

    root: Path
    config: dict[str, Any]
    #: A base URL to build against in place of the configured one.
    base_url_override: str | None = None

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_NAME

    @property
    def base_url(self) -> str:
        """The URL to root this build at, with the slash templates append to."""
        url = self.base_url_override or str(self.config['baseUrl'])
        return url.rstrip('/') + '/'

    @property
    def pandoc_options(self) -> list[str]:
        """Further Pandoc options, from the `pandoc` field of `config.yml`."""
        options: list[str] = self.config.get('pandoc', [])
        return options


@dataclass(frozen=True)
class BuildResult:
    """What a build produced."""

    pages: int
    copied: int
    #: Each page and the link in it that the build does not contain.
    broken: list[tuple[str, str]]


def find_root(start: Path) -> Path:
    """The nearest directory at or above `start` that holds a `config.yml`."""
    start = start.resolve()
    if start.is_file():
        start = start.parent
    for directory in (start, *start.parents):
        if (directory / CONFIG_NAME).is_file():
            return directory
    raise SiteError(f'no {CONFIG_NAME} in {start} or any parent directory')


def load_site(root: Path, base_url: str | None = None) -> Site:
    """Read and check the site metadata at the root of a website project."""
    path = root / CONFIG_NAME
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as error:
        raise SiteError(f'{path}: cannot be read: {error}') from error

    try:
        config: Any = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise SiteError(f'{path}: not valid YAML: {error}') from error

    if not isinstance(config, dict):
        raise SiteError(f'{path}: expected a mapping of site metadata')
    for key in ('siteName', 'baseUrl'):
        if key not in config:
            raise SiteError(f"{path}: missing required field '{key}'")

    options = config.get('pandoc', [])
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        raise SiteError(f"{path}: 'pandoc' must be a list of Pandoc options")

    return Site(root=root, config=config, base_url_override=base_url)


def find_template(directory: Path, root: Path) -> Path | None:
    """The nearest template, searching `directory` then upward as far as `root`."""
    current = directory
    while True:
        candidate = current / TEMPLATE_NAME
        if candidate.is_file():
            return candidate
        if current == root or current == current.parent:
            return None
        current = current.parent


def is_markdown(path: Path) -> bool:
    """Whether a source file is a page to convert rather than a file to copy."""
    return path.suffix.lower() in MARKDOWN_SUFFIXES


def output_path(source: Path, root: Path, build: Path) -> Path:
    """Where a source file lands in the build.

    A Markdown page is given a directory of its own -- `a/b/c.md` builds to
    `a/b/c/index.html` -- so that it is served as a path, `/a/b/c/`. An
    `index.md` is already the page for its directory and keeps its place.
    Every other file is mirrored unchanged.
    """
    relative = source.relative_to(root)
    if not is_markdown(source):
        return build / relative
    if source.stem.lower() == 'index':
        return build / relative.parent / 'index.html'
    return build / relative.with_suffix('') / 'index.html'


def source_files(root: Path, build: Path, *, special: bool = False) -> Iterator[Path]:
    """Every file under `root` that belongs in the build, in a stable order.

    With `special`, the files that drive the build are yielded as well, which
    is what watching for a change wants and copying does not.
    """
    directories = [root]
    visited = {root.resolve()}
    while directories:
        for entry in sorted(directories.pop().iterdir()):
            if entry.name.startswith('.'):
                continue
            if not special and entry.name in SPECIAL_NAMES:
                continue
            if entry.is_dir():
                # Resolving keeps a symlink from walking its own parent forever.
                resolved = entry.resolve()
                if entry != build and resolved not in visited:
                    visited.add(resolved)
                    directories.append(entry)
            elif entry.is_file():
                yield entry


def convert(
    source: Path, destination: Path, site: Site, link_filter: Path, link_log: Path
) -> None:
    """Run one Markdown file through Pandoc to produce an HTML page."""
    command = ['pandoc', '--standalone']
    template = find_template(source.parent, site.root)
    if template is not None:
        command += ['--template', str(template)]
    command += [
        '--metadata-file',
        str(site.config_path),
        '--metadata',
        f'baseUrl={site.base_url}',
        '--metadata',
        f'pageDir={source.parent.relative_to(site.root).as_posix()}',
        '--metadata',
        f'pageSource={source.relative_to(site.root).as_posix()}',
        '--metadata',
        f'linkLog={link_log}',
        '--lua-filter',
        str(link_filter),
    ]
    command += site.pandoc_options
    command += ['--output', str(destination), str(source)]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        raise SiteError('pandoc is not installed or not on PATH') from None
    except subprocess.CalledProcessError as error:
        raise SiteError(f'{source}: pandoc failed:\n{error.stderr.strip()}') from error


def check_links(link_log: Path, site: Site, build: Path) -> list[tuple[str, str]]:
    """The rewritten links whose targets are missing from the build."""
    if not link_log.is_file():
        return []

    base = site.base_url
    broken: dict[tuple[str, str], None] = {}
    for line in link_log.read_text(encoding='utf-8').splitlines():
        fields = line.split('\t', 2)
        if len(fields) != 3:
            continue
        page, target, url = fields
        if not url.startswith(base):
            continue
        path = url[len(base) :].split('#', 1)[0]
        # A page is served as a directory, so its URL stands for its index.
        candidate = build / path
        if path == '' or path.endswith('/'):
            candidate = candidate / 'index.html'
        if not candidate.exists():
            broken[(page, target)] = None
    return list(broken)


def build_site(site: Site, build: Path) -> BuildResult:
    """Mirror the site into `build`, converting its Markdown pages."""
    pages = 0
    copied = 0
    sources: dict[Path, Path] = {}
    with tempfile.TemporaryDirectory() as scratch:
        link_filter = Path(scratch) / 'links.lua'
        link_filter.write_text(LINK_FILTER, encoding='utf-8')
        link_log = Path(scratch) / 'links.tsv'

        for source in source_files(site.root, build):
            destination = output_path(source, site.root, build)
            if destination in sources:
                raise SiteError(
                    f'{source} and {sources[destination]} both build to {destination}'
                )
            sources[destination] = source
            destination.parent.mkdir(parents=True, exist_ok=True)
            if is_markdown(source):
                convert(source, destination, site, link_filter, link_log)
                pages += 1
            else:
                try:
                    shutil.copy2(source, destination)
                except OSError as error:
                    raise SiteError(f'{source}: cannot be copied: {error}') from error
                copied += 1

        broken = check_links(link_log, site, build)

    return BuildResult(pages=pages, copied=copied, broken=broken)


def remove_build(build: Path) -> None:
    """Delete the build directory, if it is there at all."""
    try:
        shutil.rmtree(build)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise SiteError(f'{build}: cannot be removed: {error}') from error


def report(site: Site, build: Path, result: BuildResult) -> int:
    """Say what a build produced, and name any link it left dangling."""
    print(
        f'{site.config["siteName"]}: {result.pages} pages, '
        f'{result.copied} files -> {build}'
    )
    for page, target in result.broken:
        print(f"simple-website: {page}: nothing built for '{target}'", file=sys.stderr)
    return 1 if result.broken else 0


def source_stamps(root: Path, build: Path) -> dict[Path, tuple[int, int]]:
    """A snapshot of the files a build reads, to compare against a later one."""
    stamps: dict[Path, tuple[int, int]] = {}
    for source in source_files(root, build, special=True):
        try:
            status = source.stat()
        except OSError:
            # A file that has gone missing is itself a change, so leaving it
            # out of the snapshot says what it should.
            continue
        stamps[source] = (status.st_mtime_ns, status.st_size)
    return stamps


def with_reload(page: bytes) -> bytes:
    """A page with the reload script added to the end of its body."""
    script = (RELOAD_SCRIPT % RELOAD_PATH).encode('utf-8')
    end = None
    for match in BODY_END.finditer(page):
        end = match.start()
    if end is None:
        return page + script
    return page[:end] + script + page[end:]


class Reloader:
    """Builds a served site again as its files change, counting the builds.

    Every page `serve` sends listens for that count; when it moves, the page
    reloads itself. A build that fails leaves the count alone, so the browser
    keeps the last page that built while the error goes to the terminal.
    """

    def __init__(self, root: Path, build: Path, base_url: str) -> None:
        self.root = root
        self.build = build
        self.base_url = base_url
        # A mark of this run of the server, so that a page which comes back to
        # a server that has been restarted can tell that it has missed one.
        self.run = time.time_ns()
        self._moved = threading.Condition()
        self._builds = 0

    @property
    def builds(self) -> int:
        """How many times the site has been built again since it was served."""
        with self._moved:
            return self._builds

    def stamp(self, builds: int) -> str:
        """What a page is told it has heard: this run, and what it built at."""
        return f'{self.run}-{builds}'

    def wait(self, builds: int, timeout: float) -> int:
        """The build count, waiting up to `timeout` seconds for it to move."""
        with self._moved:
            self._moved.wait_for(lambda: self._builds != builds, timeout)
            return self._builds

    def watch(self) -> None:
        """Build again whenever a file changes; meant to run in a thread."""
        stamps = source_stamps(self.root, self.build)
        while True:
            time.sleep(POLL_SECONDS)
            current = source_stamps(self.root, self.build)
            if current == stamps:
                continue
            # Taking the snapshot before the build means a file saved while it
            # runs is seen as a change by the next look, as it should be.
            stamps = current
            self.rebuild()

    def rebuild(self) -> None:
        """Build the site again, and tell the pages that are listening."""
        try:
            # Reading the config again picks up an edit to the site metadata.
            site = load_site(self.root, self.base_url)
            result = build_site(site, self.build)
        except (SiteError, OSError) as error:
            print(f'simple-website: {error}', file=sys.stderr)
            return
        report(site, self.build, result)
        with self._moved:
            self._builds += 1
            self._moved.notify_all()


class ReloadHandler(SimpleHTTPRequestHandler):
    """Serves the build, with the reload script added to every page it sends."""

    def __init__(self, *args: Any, reloader: Reloader, **kwargs: Any) -> None:
        # Set first: the base class answers the request as it is built.
        self.reloader = reloader
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        if self.request_path() == RELOAD_PATH:
            self.announce_builds()
            return
        super().do_GET()

    def send_head(self) -> IO[bytes] | None:
        """Hand the base class what to send, scripted if it is a page."""
        page = self.page_file()
        if page is None:
            return super().send_head()
        try:
            body = with_reload(page.read_bytes())
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, 'File not found')
            return None
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        # A page kept in the browser's cache would be a page that has stopped
        # listening for a rebuild, so each one is sent afresh.
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        return io.BytesIO(body)

    def announce_builds(self) -> None:
        """Hold the connection open, writing a notice for every build."""
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

        # A page coming back after a lost connection says which build it last
        # heard of; if that is not where the site stands now -- it has been
        # built since, or this is a server that has been restarted under it --
        # then the page missed a notice and is told to reload at once.
        heard = self.headers.get('Last-Event-ID')
        builds = self.reloader.builds
        try:
            if heard is not None and heard != self.reloader.stamp(builds):
                self.write_event('reload', builds)
            else:
                self.write_event('standing', builds)
            while True:
                current = self.reloader.wait(builds, HEARTBEAT_SECONDS)
                if current == builds:
                    self.push(': waiting\n\n')  # A comment, which a page ignores.
                else:
                    builds = current
                    self.write_event('reload', builds)
        except OSError:
            pass  # The page has gone away, and so may this stream.

    def write_event(self, kind: str, builds: int) -> None:
        """Write one notice of where the site stands.

        A page acts on `reload` and lets `standing` pass, but the browser
        keeps the mark either carries and offers it back as the Last-Event-ID
        of a connection it makes again, which is how a page that has missed a
        build while away is told to reload as soon as it returns.
        """
        stamp = self.reloader.stamp(builds)
        self.push(f'id: {stamp}\nevent: {kind}\ndata: {builds}\n\n')

    def push(self, message: str) -> None:
        """Write one message to a page listening for builds."""
        self.wfile.write(message.encode('utf-8'))
        self.wfile.flush()

    def page_file(self) -> Path | None:
        """The HTML file this request asks for, if a page is what it asks for."""
        target = Path(self.translate_path(self.path))
        if target.is_dir():
            # A directory asked for without the slash is a redirect, which is
            # the base class's business rather than ours.
            if not self.request_path().endswith('/'):
                return None
            for name in INDEX_NAMES:
                if (target / name).is_file():
                    target = target / name
                    break
            else:
                return None
        if target.suffix.lower() in HTML_SUFFIXES and target.is_file():
            return target
        return None

    def request_path(self) -> str:
        """The path asked for, without any query string or fragment."""
        return self.path.split('?', 1)[0].split('#', 1)[0]

    def log_request(self, code: Any = '-', size: Any = '-') -> None:
        """Log every request but the announcements, which are only noise."""
        if self.request_path() != RELOAD_PATH:
            super().log_request(code, size)


def command_init(path: Path) -> int:
    """Set up a new website project."""
    path.mkdir(parents=True, exist_ok=True)
    if (path / CONFIG_NAME).exists():
        raise SiteError(f'{path / CONFIG_NAME}: a website project is already set up')

    written = []
    starters = (
        (CONFIG_NAME, STARTER_CONFIG),
        (TEMPLATE_NAME, STARTER_TEMPLATE),
        ('index.md', STARTER_PAGE),
    )
    for name, contents in starters:
        if not (path / name).exists():
            (path / name).write_text(contents, encoding='utf-8')
            written.append(name)

    print(f'{path}: created {", ".join(written)}')
    return 0


def command_build(path: Path, clean: bool, base_url: str | None) -> int:
    """Build the website project containing `path`."""
    site = load_site(find_root(path), base_url)
    build = site.root / BUILD_NAME
    if clean:
        remove_build(build)
    return report(site, build, build_site(site, build))


def command_serve(path: Path, port: int, reload: bool) -> int:
    """Build the site against localhost and serve it until interrupted."""
    root = find_root(path)
    base_url = f'http://localhost:{port}/'
    site = load_site(root, base_url)
    build = root / BUILD_NAME

    remove_build(build)
    report(site, build, build_site(site, build))

    handler: Callable[..., BaseHTTPRequestHandler]
    if reload:
        reloader = Reloader(root, build, base_url)
        threading.Thread(target=reloader.watch, daemon=True).start()
        handler = functools.partial(
            ReloadHandler, directory=str(build), reloader=reloader
        )
    else:
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(build))

    with ThreadingHTTPServer(('', port), handler) as server:
        watching = 'reloading as files change' if reload else 'not reloading'
        print(f'serving {base_url} ({watching}) -- Ctrl-C to stop')
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print()
    return 0


def command_clean(path: Path) -> int:
    """Delete the build directory of the website project containing `path`."""
    build = find_root(path) / BUILD_NAME
    if not build.exists():
        print(f'nothing to remove at {build}')
        return 0
    remove_build(build)
    print(f'removed {build}')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='simple-website.py',
        description='Build a static website from Markdown files with Pandoc.',
    )
    commands = parser.add_subparsers(dest='command', required=True)

    location = argparse.ArgumentParser(add_help=False)
    location.add_argument(
        'path',
        nargs='?',
        default=Path.cwd(),
        type=Path,
        help='a directory in the website project (default: the current one)',
    )

    build = commands.add_parser(
        'build', parents=[location], help=f'convert the site into {BUILD_NAME}'
    )
    build.add_argument(
        '--clean',
        action='store_true',
        help=f'delete {BUILD_NAME} first, leaving no files from an earlier build',
    )
    build.add_argument(
        '--base-url',
        metavar='URL',
        help=f'root URLs at this address instead of the one in {CONFIG_NAME}',
    )

    serve = commands.add_parser(
        'serve',
        parents=[location],
        help='build for localhost and serve it, reloading pages as files change',
    )
    serve.add_argument(
        '--port',
        type=int,
        default=DEFAULT_PORT,
        help=f'the port to serve on (default: {DEFAULT_PORT})',
    )
    serve.add_argument(
        '--no-reload',
        dest='reload',
        action='store_false',
        help='serve the site as built, without watching it or reloading pages',
    )

    commands.add_parser('clean', parents=[location], help=f'delete {BUILD_NAME}')
    commands.add_parser('init', parents=[location], help='set up a new website project')

    arguments = parser.parse_args(argv)
    try:
        if arguments.command == 'build':
            return command_build(arguments.path, arguments.clean, arguments.base_url)
        if arguments.command == 'serve':
            return command_serve(arguments.path, arguments.port, arguments.reload)
        if arguments.command == 'clean':
            return command_clean(arguments.path)
        return command_init(arguments.path)
    except (SiteError, OSError) as error:
        print(f'simple-website: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
