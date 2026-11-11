#!/usr/bin/env python3
"""Find ComfyUI knowledge in app code that should not have any.

    find_leaks.py DIR [--allow GLOB ...] [--ext .ts,.tsx,...]

Only three places may know ComfyUI: the executor, the mapper and the templates. Name them with --allow (a glob
matched against the path relative to DIR, "/" as the separator); every other source file is searched for:

    node-id-index     workflow["6"]["inputs"], prompt['3'].inputs,   a node id used as an address
                      entry.outputs["9"]
    class-type        class_type                                      the graph format itself
    comfy-endpoint    /upload/image, /object_info, /history/, "/prompt", /view?filename, ws?clientId, :8188
    node-class-name   KSampler, CLIPTextEncode, VAEDecode, CheckpointLoaderSimple, EmptyLatentImage, SaveImage,
                      LoadImage

Each hit is printed as  path:line: kind: the line.  Exit 0 = none, 1 = hits, 2 = cannot read DIR.

It is a search for patterns, not a parser. It misses a node id built at run time and custom node names, and it
reports a hit inside a comment or a string as readily as one in code. A clean run means "none of these
patterns", not "no coupling". Standard library only.
"""
import argparse
import fnmatch
import os
import re
import sys

EXTENSIONS = '.ts,.tsx,.js,.jsx,.mjs,.vue,.svelte,.py'
SKIP_DIRS = {'node_modules', '.git', 'dist', 'build', '.next', '__pycache__', '.venv', 'venv', 'env'}
PATTERNS = [
    ('node-id-index', re.compile(r'''\[\s*['"]\d+['"]\s*\]\s*(?:\??\.\s*inputs\b|\[\s*['"]inputs['"]\s*\])'''
                                 r'''|\boutputs(?:['"]\s*\])?\s*\[\s*['"]\d+['"]\s*\]''')),
    ('class-type', re.compile(r'\bclass_type\b')),
    ('comfy-endpoint', re.compile(r'''/upload/image|/object_info|/history/|/prompt['"`]|/view\?filename|ws\?clientId|:8188\b''')),
    ('node-class-name', re.compile(r'\b(?:KSampler(?:Advanced)?|CLIPTextEncode|VAEDecode|VAEEncode|CheckpointLoaderSimple|'
                                   r'EmptyLatentImage|SaveImage|LoadImage)\b')),
]


def allowed(rel, globs):
    return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g.rstrip('/') + '/*') for g in globs)


def scan_text(text):
    """[(line number, kind, line)] for one file's text; one hit per kind per line."""
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        for kind, pattern in PATTERNS:
            if pattern.search(line):
                hits.append((number, kind, line.strip()))
    return hits


def scan(root, allow=(), extensions=EXTENSIONS):
    """[(relative path, line number, kind, line)], sorted by path."""
    wanted = tuple(e if e.startswith('.') else '.' + e for e in extensions.split(',') if e)
    found = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            if not name.endswith(wanted):
                continue
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, root).replace(os.sep, '/')
            if allowed(rel, allow):
                continue
            try:
                with open(path, encoding='utf-8', errors='replace') as f:
                    text = f.read()
            except OSError:
                continue
            found.extend((rel, number, kind, line) for number, kind, line in scan_text(text))
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description='Find ComfyUI knowledge outside the executor, mapper and templates.')
    parser.add_argument('dir')
    parser.add_argument('--allow', action='append', default=[], metavar='GLOB',
                        help='a path (relative to DIR) that may know ComfyUI; repeat for more')
    parser.add_argument('--ext', default=EXTENSIONS, help=f'file extensions to read (default {EXTENSIONS})')
    args = parser.parse_args(argv)
    if not os.path.isdir(args.dir):
        print(f'find_leaks: {args.dir} is not a folder', file=sys.stderr)
        return 2
    found = scan(args.dir, args.allow, args.ext)
    for rel, number, kind, line in found:
        print(f'{rel}:{number}: {kind}: {line[:160]}')
    files = len({rel for rel, *_ in found})
    print(f'{len(found)} leak{"" if len(found) == 1 else "s"} in {files} file{"" if files == 1 else "s"}'
          if found else 'no leaks found (patterns only; see --help)')
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
