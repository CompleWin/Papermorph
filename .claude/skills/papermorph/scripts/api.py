#!/usr/bin/env python3
"""Look up the engine's functions by name instead of reading engine.js by line ranges.

    uv run api.py                     # index: sections, functions with parameters, line numbers
    uv run api.py M chip quiz         # each definition in full, with the comment above it
    uv run api.py --engine site/<book>/lib/engine.js NAME     # a book's own copy

The index lists top-level definitions: functions (arrow functions included) with their
parameters, defaults left out, and other constants and variables by name. Names are exact
(M and m differ). Without --engine the script reads the engine in this skill; a book's copy
may add its own shared helpers.
"""
import argparse
import difflib
import re
import sys
from pathlib import Path

DEFAULT_ENGINE = Path(__file__).resolve().parent.parent / "assets" / "engine" / "engine.js"
SECTION = re.compile(r"/\* -+ (.*?) -+ \*/")
FUNCTION = re.compile(r"(?:async\s+)?function\s*\*?\s*([\w$]+)\s*\(")
DECLARATION = re.compile(r"(?:const|let|var)\s+")
DECLARATOR = re.compile(r"\s*([\w$]+)\s*=(?![=>])\s*")
OPENERS, CLOSERS = "([{", ")]}"
REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^")
REGEX_AFTER_WORDS = {"return", "typeof", "in", "of", "case", "do", "else", "void", "delete", "new", "instanceof", "yield", "await"}


class Definition:
    def __init__(self, name, params, first, last, section):
        self.name = name
        self.params = params          # None for a value that is not a function
        self.first, self.last = first, last
        self.section = section


def code_only(src):
    """The source with comments, strings, template text and regex literals blanked out.

    Newlines and length are kept, so positions and line numbers match the source;
    brackets and commas left in the result are code.
    """
    out = list(src)
    length = len(src)
    open_templates = []     # brace depth inside each open ${…}; its template resumes when it closes
    last = ""               # last code character, to tell a regex from a division

    def blank(start, end):
        for pos in range(start, end):
            if out[pos] != "\n":
                out[pos] = " "

    def skip_template_text(pos):
        """Blank template text from pos; return the position after the closing ` or ${, and whether ${ opened."""
        start = pos
        while pos < length:
            if src[pos] == "\\":
                pos += 2
            elif src[pos] == "`":
                blank(start, pos + 1)
                return pos + 1, False
            elif src.startswith("${", pos):
                blank(start, pos + 2)
                return pos + 2, True
            else:
                pos += 1
        blank(start, length)
        return length, False

    pos = 0
    while pos < length:
        char = src[pos]
        if src.startswith("//", pos):
            end = src.find("\n", pos)
            end = length if end < 0 else end
            blank(pos, end)
            pos = end
        elif src.startswith("/*", pos):
            end = src.find("*/", pos + 2)
            end = length if end < 0 else end + 2
            blank(pos, end)
            pos = end
        elif char in "'\"":
            end = pos + 1
            while end < length and src[end] not in (char, "\n"):
                end += 2 if src[end] == "\\" else 1
            blank(pos, end + 1)
            pos, last = end + 1, "x"
        elif char == "`" or (char == "}" and open_templates and open_templates[-1] == 0):
            if char == "}":
                open_templates.pop()
                blank(pos, pos + 1)
            pos, opened = skip_template_text(pos + 1)
            if opened:
                open_templates.append(0)
            last = "x"
        elif char == "/" and (not last or last in REGEX_AFTER or word_before(src, pos) in REGEX_AFTER_WORDS):
            end, in_class = pos + 1, False
            while end < length and src[end] != "\n" and (src[end] != "/" or in_class):
                if src[end] == "\\":
                    end += 1
                elif src[end] in "[]":
                    in_class = src[end] == "["
                end += 1
            while end + 1 < length and src[end + 1].isalpha():     # flags
                end += 1
            blank(pos, end + 1)
            pos, last = end + 1, "x"
        else:
            if open_templates and char in "{}":
                open_templates[-1] += 1 if char == "{" else -1
            if not char.isspace():
                last = char
            pos += 1
    return "".join(out)


def word_before(src, pos):
    match = re.search(r"([\w$]+)\s*$", src[max(0, pos - 20):pos])
    return match[1] if match else ""


def bracket_balance(code_line):
    return sum(code_line.count(b) for b in OPENERS) - sum(code_line.count(b) for b in CLOSERS)


def closing_bracket(code, opening):
    """Position of the bracket that closes the one at opening."""
    depth = 0
    for pos in range(opening, len(code)):
        if code[pos] in OPENERS:
            depth += 1
        elif code[pos] in CLOSERS:
            depth -= 1
            if depth == 0:
                return pos
    return len(code) - 1


def top_level_commas(code, start, end):
    depth, commas = 0, []
    for pos in range(start, end):
        if code[pos] in OPENERS:
            depth += 1
        elif code[pos] in CLOSERS:
            depth -= 1
        elif code[pos] == "," and depth == 0:
            commas.append(pos)
    return commas


def params_without_defaults(src, code, start, end):
    """The parameter text between start and end with every `= default` removed."""
    kept, depth = [], 0
    default_depth = None        # bracket depth of the default value being skipped
    for pos in range(start, end):
        char = code[pos]
        if default_depth == depth and (char == "," or char in CLOSERS):
            default_depth = None    # the default ends where its parameter or the enclosing bracket does
        if char in OPENERS:
            depth += 1
        elif char in CLOSERS:
            depth -= 1
        if default_depth is not None:
            continue
        if char == "=" and code[pos + 1:pos + 2] not in ("=", ">") and code[pos - 1:pos] not in ("=", "!", "<", ">"):
            default_depth = depth
            while kept and kept[-1] == " ":
                kept.pop()
            continue
        kept.append(src[pos])
    text = re.sub(r"\s+", " ", "".join(kept)).strip()
    return re.sub(r"([\[{]) | ([\]}])", r"\1\2", text)


def arrow_params(src, code, start, end):
    """Parameters of the arrow or function expression at start, or None for another value."""
    text = code[start:end]
    single = re.match(r"(?:async\s+)?([\w$]+)\s*=>", text)
    if single:
        return single[1]
    opening = re.match(r"(?:async\s+)?(function\s*[\w$]*\s*)?\(", text)
    if not opening:
        return None
    open_at = start + opening.end() - 1
    close_at = closing_bracket(code, open_at)
    if not opening[1] and not re.match(r"\s*=>", code[close_at + 1:end]):
        return None             # a call or a parenthesised value, not a function
    return params_without_defaults(src, code, open_at + 1, close_at)


def parse(src):
    """Top-level definitions and the section headers, with 1-based line numbers."""
    code = code_only(src)
    lines, code_lines = src.split("\n"), code.split("\n")
    line_starts = [0]
    for line in lines:
        line_starts.append(line_starts[-1] + len(line) + 1)
    definitions, sections, section = [], [], ""
    depth, index = 0, 0
    while index < len(lines):
        header = SECTION.fullmatch(lines[index].strip())
        if depth == 0 and header and not lines[index][0].isspace():
            section = header[1]
            sections.append((section, index + 1))
        line_code = code_lines[index]
        if depth != 0 or not (FUNCTION.match(line_code) or DECLARATION.match(line_code)):
            depth += bracket_balance(line_code)
            index += 1
            continue
        last, balance = index, bracket_balance(line_code)
        # The statement runs on while brackets are open, or onto an indented line after an unfinished one.
        while last + 1 < len(lines) and (balance > 0 or (
                not code_lines[last].rstrip().endswith((";", "}")) and lines[last + 1][:1] in (" ", "\t"))):
            last += 1
            balance += bracket_balance(code_lines[last])
        statement_start, statement_end = line_starts[index], line_starts[last + 1] - 1
        found = []
        function = FUNCTION.match(line_code)
        if function:
            opening = statement_start + function.end() - 1
            found.append((function[1], params_without_defaults(src, code, opening + 1, closing_bracket(code, opening))))
        else:
            body_start = statement_start + DECLARATION.match(line_code).end()
            bounds = [body_start - 1] + top_level_commas(code, body_start, statement_end) + [statement_end]
            for after, until in zip(bounds, bounds[1:]):        # one declarator between each pair of commas
                declarator = DECLARATOR.match(code, after + 1, until)
                if declarator:
                    found.append((declarator[1], arrow_params(src, code, declarator.end(), until)))
        for name, params in found:
            definitions.append(Definition(name, params, index + 1, last + 1, section))
        index = last + 1
    return definitions, sections


def comment_start(lines, first):
    """Index of the first `//` line right above the definition on line first."""
    start = first - 1
    while start > 0 and lines[start - 1].lstrip().startswith("//"):
        start -= 1
    return start


def print_index(engine, definitions, sections, line_count):
    print(f"{engine.name} ({line_count} lines). `api.py NAME...` prints definitions in full.")
    for section, line in [("", 0)] + sections:
        members = [definition for definition in definitions if definition.section == section]
        if not members:
            continue
        if section:
            print(f"\n## {section}  L{line}")
        values = [member.name for member in members if member.params is None]
        if values:
            print("values: " + ", ".join(values))
        for member in members:
            if member.params is not None:
                print(f"{member.name}({member.params})  L{member.first}")


def print_definitions(names, definitions, lines):
    """Print each named definition; return the names that are not defined."""
    by_name = {}
    for definition in definitions:
        by_name.setdefault(definition.name, []).append(definition)
    unknown = []
    for name in names:
        for definition in by_name.get(name, []):
            first, last = definition.first, definition.last
            span = f"L{first}" if first == last else f"L{first}-{last}"
            where = f"  ({definition.section})" if definition.section else ""
            print(f"--- {name}  {span}{where}")
            print("\n".join(lines[comment_start(lines, first):last]))
        if name not in by_name:
            unknown.append(name)
    for name in unknown:
        similar = [known for known in by_name if known.lower() == name.lower()]
        similar += [known for known in difflib.get_close_matches(name, by_name, n=6, cutoff=0.6) if known not in similar]
        hint = f"; similar: {', '.join(similar)}" if similar else "; run without names for the index"
        print(f"unknown name: {name}{hint}", file=sys.stderr)
    return unknown


def main():
    # Captured output (a pipe) uses the system code page on Windows; comments may be in any language.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*", help="functions or values to print in full")
    parser.add_argument("--engine", type=Path, default=DEFAULT_ENGINE, help="engine.js to read (default: this skill's)")
    args = parser.parse_args()
    if not args.engine.is_file():
        parser.error(f"no engine at {args.engine}")
    src = args.engine.read_text(encoding="utf-8-sig")
    definitions, sections = parse(src)
    if not args.names:
        print_index(args.engine, definitions, sections, len(src.splitlines()))
    elif print_definitions(args.names, definitions, src.split("\n")):
        sys.exit(1)


if __name__ == "__main__":
    main()
