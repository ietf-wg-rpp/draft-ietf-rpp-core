#!/usr/bin/env python3
"""endpoints.py — derive, verify and regenerate RPP endpoint tables.

Derives the RPP endpoint set by applying the HTTP Mapping Rules of the core
draft (Rule 1 collection segment, Rule 2 uniform operations, Rule 3 Direct
Access sub-resources, Rules 4-6 process operations and listing) to the
structured Data Objects export (.work/draft-ietf-rpp-data-objects.yaml).

Checks performed (default: all of them):

  table     the table under "## Derived Endpoint Reference" equals the
            derived endpoint set (compared by HTTP method + URL path; labels
            are compared too and reported as warnings)
  examples  every request line and every Location / Content-Location / Link
            URI in ```http-message (and ```http) example blocks addresses a
            derived endpoint, with an allowed method, a process type the
            owning Data Object supports, and query parameters the operation
            defines (Rule 7); every operation parameter the data objects
            define is used in at least one example request

Usage:
  .scripts/endpoints.py                    # run all checks
  .scripts/endpoints.py --check table      # only verify the table
  .scripts/endpoints.py --check examples   # only verify the examples
  .scripts/endpoints.py --print            # print the regenerated table
  .scripts/endpoints.py --write            # regenerate the table in place

Derived order: objects in YAML order (Data Objects, then Process Objects),
each object's operations ordered read, create, query, update, delete,
availability, then custom operations, then list.

Regenerated table (--print, --write): a derived row replaces the current row
with the same Operation label, else the same method + URL path, in place;
otherwise it is added at the end of its object's section (or as a new section
after the preceding object's). Current rows matching no derived row are kept
at the end. Objects or operations out of order are reported as warnings,
never reordered.

Exit status: 0 when all requested checks pass (warnings allowed), 1 when an
error was found, 2 on bad input.
"""

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import yaml

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DRAFT = REPO / "src" / "draft-ietf-rpp-core.md"
DEFAULT_MODEL = REPO / ".work" / "draft-ietf-rpp-data-objects.yaml"
TABLE_HEADING = "## Derived Endpoint Reference"
# A TODO naming an issue, directly after an example block or the table, turns
# the errors it covers into warnings.
TODO_ISSUE_RE = re.compile(r"^(?:A>\s*)?(?:<!--\s*)?\**TODO\b.*?(https?://\S+/issues/\d+)")

# Discovery has no Unique Identifier and is not served below the base URL;
# the core draft maps it to this fixed well-known path (Discoverability).
WELL_KNOWN_DISCOVERY = "/.well-known/rpp"

UNIFORM = ("create", "read", "query", "update", "delete", "availability")
# Availability is not in the Rule 2 table; its mapping is the one given in
# "Availability for Creation": HEAD or GET on the instance's sub-path.
AVAILABILITY_SEGMENT = "availability"
# Row order of an object's operations in the table: OP_ORDER, then custom
# (Rule 5) operations in document order, then the Rule 6 "list" row.
OP_ORDER = ("read", "create", "query", "update", "delete", "availability")
PROCESS_INSTANCE = "latest"


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

@dataclass
class Endpoint:
    label: str
    methods: tuple          # e.g. ("PUT", "PATCH")
    path: str               # URI template, e.g. "/domainNames/{id}"
    params: set = field(default_factory=set)   # Rule 7 operation parameters
    process: str = ""       # process DA element identifier, if any
    group: int = 0          # position of the owning object in the YAML
    raw: str = ""           # original table row, for parsed rows
    var_owners: tuple = ()  # object each path placeholder identifies, in order

    @property
    def rank(self):
        op = self.label.rsplit(": ", 1)[-1]
        if op == "list":
            return len(OP_ORDER) + 1
        return OP_ORDER.index(op) if op in OP_ORDER else len(OP_ORDER)

    def key(self):
        """Comparison key; placeholder names do not matter ({id} == {name})."""
        return (frozenset(self.methods), re.sub(r"\{[^}]+\}", "{}", self.path))


def plural(word, irregular=None):
    """Rule 1 plural(): English regular plural unless defined as irregular."""
    if irregular and word in irregular:
        return irregular[word]
    if re.search(r"[^su]s$", word):
        return word                 # already plural, e.g. "users"
    if re.search(r"(s|x|z|ch|sh)$", word):
        return word + "es"
    if re.search(r"[^aeiou]y$", word):
        return word[:-1] + "ies"
    return word + "s"


def inner_type(data_type):
    """'Aggregation[Transfer Process Object]' -> 'Transfer Process Object'."""
    m = re.search(r"\[([^\]]+)\]", data_type)
    return (m.group(1) if m else data_type).strip()


def multi(cardinality):
    """True when a cardinality allows more than one value (0+, 1+, 2-5 ...)."""
    c = str(cardinality).strip()
    if c.endswith("+"):
        return True
    m = re.fullmatch(r"\d+-(\d+)", c)
    return bool(m and int(m.group(1)) > 1) or (c.isdigit() and int(c) > 1)


def uniform_verb(op):
    """Map a Data Object operation to its Rule 2 uniform verb, or None.

    The operation Name carries the verb ('Create', 'Read Operation'); the
    Identifier may be prefixed ('transferCreate')."""
    ident = op["identifier"]
    if ident in UNIFORM:
        return ident
    name = re.sub(r"\s+Operation$", "", op.get("name", "")).strip().lower()
    return name if name in UNIFORM else None


def op_params(op):
    out = set()
    for item in op.get("input") or []:
        if item.get("kind") == "Parameters":
            out |= {d["identifier"] for d in item.get("definitions") or []}
    return out


def short_name(obj, suffix):
    return re.sub(rf"\s+{suffix}$", "", obj["name"])


class Model:
    def __init__(self, data, irregular=None):
        self.irregular = irregular or {}
        self.objects = {}            # Object Name -> object
        for kind in ("components", "processes", "resources"):
            for obj in data.get(kind) or []:
                self.objects[obj["name"]] = obj
        self.resources = data.get("resources") or []
        self.processes = data.get("processes") or []
        self.warnings = []
        self.errors = []
        self.endpoints = []
        # container template -> owner resource, for every container that
        # carries a Processes Direct Access element
        self.process_owners = {}
        self._derive()

    # -- helpers -----------------------------------------------------------

    def resolve(self, name):
        """Resolve an object reference; 'Renew Process' -> 'Renew Process Object'."""
        return self.objects.get(name) or self.objects.get(name + " Object")

    def ancestry(self, obj):
        while obj:
            yield obj
            obj = self.resolve(obj.get("extends") or "") if obj.get("extends") else None

    def descendants(self, obj):
        return [o for o in self.processes
                if o is not obj and any(a is obj for a in self.ancestry(o))]

    # -- derivation ----------------------------------------------------------

    def _derive(self):
        res_by_name = {r["name"]: r for r in self.resources}
        nested = {}                   # child resource identifier -> (parent, element)
        for r in self.resources:
            for el in r.get("elements") or []:
                target = res_by_name.get(inner_type(el["data_type"]))
                if el.get("direct_access") and target:
                    nested[target["identifier"]] = (r, el)

        self.processes_component = next(
            (o for o in self.objects.values() if o["identifier"] == "processes"
             and o["object_type"] == "Component"), None)

        for r in self.resources:
            if r["identifier"] in nested:
                continue                        # emitted below its parent
            if not r.get("unique_identifier"):
                self._discovery(r)
                continue
            self._resource(r, "/" + plural(r["identifier"], self.irregular), ())

        self._processes()
        # Objects in YAML order (Data Objects, then Process Objects), each
        # object's operations in OP_ORDER; sorted() is stable.
        self.endpoints = sorted(self.endpoints, key=lambda ep: (ep.group, ep.rank))

    def _add(self, obj, ep):
        if obj in self.resources:
            ep.group = self.resources.index(obj)
        elif obj in self.processes:
            ep.group = len(self.resources) + self.processes.index(obj)
        else:
            ep.group = len(self.resources) + len(self.processes)
        self.endpoints.append(ep)

    def _discovery(self, r):
        if r["identifier"] != "discovery":
            self.warnings.append(
                f"resource {r['identifier']!r} has no Unique Identifier and no "
                "known fixed endpoint; skipped")
            return
        for op in r.get("operations") or []:
            if uniform_verb(op) == "read":
                self._add(r, Endpoint(
                    f"{short_name(r, 'Data Object')}: read", ("GET",),
                    WELL_KNOWN_DISCOVERY, op_params(op)))

    def _resource(self, r, collection, owners):
        """owners: object names of the placeholders already in collection."""
        label = short_name(r, "Data Object")
        # The placeholder is the Unique Identifier, verbatim.
        id_var = "{" + r["unique_identifier"] + "}"
        item = f"{collection}/{id_var}"
        if id_var in collection:
            self.errors.append(
                f"{item}: placeholder {id_var} occurs twice; the Unique Identifier of "
                f"{r['name']} clashes with that of {owners[placeholders(collection).index(id_var)]}")
        item_owners = owners + (r["name"],)

        def add(ep):
            ep.var_owners = item_owners if ep.path.startswith(item) else owners
            self._add(r, ep)

        for op in r.get("operations") or []:
            verb = uniform_verb(op)
            params = op_params(op)
            if verb == "create":
                add(Endpoint(f"{label}: create", ("POST",), collection, params))
            elif verb == "query":
                add(Endpoint(f"{label}: query", ("GET",), collection, params))
            elif verb == "read":
                add(Endpoint(f"{label}: read", ("GET",), item, params))
            elif verb == "update":
                add(Endpoint(f"{label}: update", ("PUT", "PATCH"), item, params))
            elif verb == "delete":
                add(Endpoint(f"{label}: delete", ("DELETE",), item, params))
            elif verb == "availability":
                add(Endpoint(f"{label}: availability", ("HEAD", "GET"),
                             f"{item}/{AVAILABILITY_SEGMENT}", params))
            else:
                self.warnings.append(
                    f"{r['identifier']}: operation {op['identifier']!r} is not a "
                    "uniform interface operation; no Data Object mapping rule applies")

        for el in r.get("elements") or []:
            if not el.get("direct_access"):
                continue
            target = self.resolve(inner_type(el["data_type"]))
            if target is self.processes_component:
                self.process_owners[item] = r
                continue
            if target is None or target.get("object_type") != "Resource":
                self.warnings.append(
                    f"{r['identifier']}.{el['identifier']}: Direct Access target "
                    f"{el['data_type']!r} is not a Data Object; skipped")
                continue
            if multi(el["cardinality"]):
                if not target.get("unique_identifier"):
                    self.warnings.append(
                        f"{r['identifier']}.{el['identifier']}: cardinality "
                        f"{el['cardinality']} but {target['name']} has no Unique Identifier")
                self._resource(target, f"{item}/{plural(el['identifier'], self.irregular)}",
                               item_owners)
            else:
                self.warnings.append(
                    f"{r['identifier']}.{el['identifier']}: single-valued Direct "
                    "Access Data Object not mapped (no operations defined for it)")

    def _processes(self):
        pc = self.processes_component
        if pc is None:
            return
        base = "/{collection}/{id}/processes"
        self.process_types = {}       # DA element identifier -> base process object
        for el in pc.get("elements") or []:
            if not el.get("direct_access"):
                continue
            proc = self.resolve(inner_type(el["data_type"]))
            if proc is None:
                self.warnings.append(f"processes.{el['identifier']}: unknown type {el['data_type']!r}")
                continue
            self.process_types[el["identifier"]] = proc
            label = short_name(proc, "Process Object")
            seg = plural(el["identifier"], self.irregular) if multi(el["cardinality"]) else el["identifier"]
            coll = f"{base}/{seg}"
            inst = f"{coll}/{PROCESS_INSTANCE}"

            # Operations of the process and of every object extending it,
            # first definition wins, in document order.
            ops, seen = [], set()
            for obj in [proc] + self.descendants(proc):
                for op in obj.get("operations") or []:
                    k = uniform_verb(op) or op["identifier"]
                    if k not in seen:
                        seen.add(k)
                        ops.append(op)
            for op in ops:
                verb = uniform_verb(op)
                params = op_params(op)
                if verb == "create":
                    ep = Endpoint(f"{label}: create", ("POST",), coll, params, el["identifier"])
                elif verb == "read":
                    ep = Endpoint(f"{label}: read", ("GET",), inst, params, el["identifier"])
                elif verb == "update":
                    ep = Endpoint(f"{label}: update", ("PUT", "PATCH"), inst, params, el["identifier"])
                elif verb == "delete":
                    ep = Endpoint(f"{label}: delete", ("DELETE",), inst, params, el["identifier"])
                elif verb == "query":
                    ep = Endpoint(f"{label}: query", ("GET",), coll, params, el["identifier"])
                else:   # Rule 5
                    ep = Endpoint(f"{label}: {op['identifier']}", ("POST",),
                                  f"{inst}/{op['identifier']}", params, el["identifier"])
                self._add(proc, ep)
            # Rule 6, per process type
            self._add(proc, Endpoint(f"{label}: list", ("GET",), coll, set(), el["identifier"]))
        # Rule 6, all process types
        self._add(None, Endpoint("Processes: list", ("GET",), base))
        for ep in self.endpoints:
            if ep.path.startswith(base):
                ep.var_owners = ("owner Data Object", "owner Data Object")

    # -- process support per owner --------------------------------------------

    def supported_process_types(self, owner):
        """DA element identifiers of the process types a Data Object supports:
        its inherited-process list plus Process Objects it owns (extensions)."""
        objs = [self.resolve(s["name"]) for s in owner.get("supported_processes") or []]
        objs += [p for p in self.processes if p.get("owner") == owner["identifier"]]
        out = set()
        for obj in filter(None, objs):
            for anc in self.ancestry(obj):
                for ident, base in self.process_types.items():
                    if base is anc:
                        out.add(ident)
        return out


# --------------------------------------------------------------------------
# Table rendering / parsing
# --------------------------------------------------------------------------

def fmt_methods(methods):
    return " or ".join(f"`{m}`" for m in methods)


def render_row(ep):
    return f'| {ep.label} | {fmt_methods(ep.methods)} | `"{ep.path}"` |'


def match_rows(endpoints, current):
    """Pair current table rows with derived endpoints: by Operation label
    first, then by method + URL path; each row matches at most once.
    Returns {index in current: index in endpoints}."""
    matched = {}
    for pass_ in ("label", "key"):
        for i, ep in enumerate(endpoints):
            if i in matched.values():
                continue
            for j, c in enumerate(current):
                if j not in matched and (c.label == ep.label if pass_ == "label"
                                         else c.key() == ep.key()):
                    matched[j] = i
                    break
    return matched


def order_warnings(endpoints, idxs):
    """Warnings for a table whose rows map to derived endpoints idxs (in row
    order): objects out of YAML order or split up, operations out of order."""
    out = []
    groups = [endpoints[i].group for i in idxs]
    firsts = list(dict.fromkeys(groups))
    name = lambda g: next(ep.label.rsplit(": ", 1)[0] for ep in endpoints if ep.group == g)
    for a, b in zip(firsts, firsts[1:]):
        if a > b:
            out.append(f"object {name(b)!r} is out of order (after {name(a)!r})")
    for g in firsts:
        rows = [k for k, gg in enumerate(groups) if gg == g]
        if rows != list(range(rows[0], rows[-1] + 1)):
            out.append(f"object {name(g)!r} rows are not contiguous")
        ops = [idxs[k] for k in rows]
        for a, b in zip(ops, ops[1:]):
            if a > b:
                out.append(f"operation {endpoints[b].label!r} is out of order "
                           f"(after {endpoints[a].label!r})")
    return out


def placeholders(path):
    return re.findall(r"\{[^}]+\}", path)


def placeholder_warnings(endpoints, current, matched):
    """Warnings for matched rows whose path placeholders differ from the
    derived ones, i.e. from the Unique Identifier of the object addressed."""
    out = []
    for j, i in sorted(matched.items()):
        cur, ep = current[j], endpoints[i]
        if cur.key() != ep.key():
            continue
        for c, d, owner in zip(placeholders(cur.path), placeholders(ep.path), ep.var_owners):
            if c != d:
                out.append(f"{cur.label}: placeholder {c} in {cur.path!r} should be {d} "
                           f"(Unique Identifier of {owner})")
    return out


def render_table(endpoints, current=None):
    """Merge the derived rows into the current table rows.

    A matched row (see match_rows) is replaced where it is. An unmatched
    derived row is inserted at the end of its object's section (the
    contiguous rows starting at the object's first row); an object without a
    section gets a new one after the closest preceding object's section.
    Current rows matching no derived row are kept verbatim at the end.
    Returns (lines, warnings); the warnings describe the current order."""
    current = current or []
    matched = match_rows(endpoints, current)
    warnings = order_warnings(endpoints, [matched[j] for j in sorted(matched)])
    warnings += placeholder_warnings(endpoints, current, matched)
    rows = [matched[j] for j in range(len(current)) if j in matched]   # endpoint idxs
    for i, ep in enumerate(endpoints):
        if i in rows:
            continue
        groups = [endpoints[r].group for r in rows]
        if ep.group in groups:
            k = groups.index(ep.group)
            while k < len(rows) and groups[k] == ep.group:
                k += 1
        else:
            before = [g for g in groups if g < ep.group]
            if before:
                k = groups.index(max(before))
                while k < len(rows) and groups[k] == max(before):
                    k += 1
            else:
                k = 0
        rows.insert(k, i)
    lines = ["| Operation | HTTP Method | URL path |", "|---|---|---|"]
    lines += [render_row(endpoints[i]) for i in rows]
    lines += [c.raw for j, c in enumerate(current) if j not in matched]
    return lines, warnings


def find_table(lines):
    """Return (start, end) line indexes (end exclusive) of the first pipe
    table following TABLE_HEADING, before the next heading."""
    try:
        h = next(i for i, l in enumerate(lines) if l.strip() == TABLE_HEADING)
    except StopIteration:
        raise SystemExit(f"error: heading {TABLE_HEADING!r} not found")
    i = h + 1
    while i < len(lines) and not lines[i].startswith("|"):
        if lines[i].startswith("#"):
            raise SystemExit(f"error: no table under {TABLE_HEADING!r}")
        i += 1
    j = i
    while j < len(lines) and lines[j].startswith("|"):
        j += 1
    if i == len(lines):
        raise SystemExit(f"error: no table under {TABLE_HEADING!r}")
    return i, j


def table_todos(lines, end):
    """Return [(line_no, text, issue)] for the TODO lines with an issue link
    directly following the table ending at line index end (blank lines
    skipped)."""
    out = []
    i = end
    while i < len(lines):
        if lines[i].strip():
            m = TODO_ISSUE_RE.match(lines[i])
            if not m:
                break
            out.append((i + 1, lines[i], m.group(1)))
        i += 1
    return out


def parse_table(rows):
    out = []
    for row in rows[2:]:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        methods = tuple(re.findall(r"`([A-Z]+)`", cells[1]))
        path = cells[2].strip("`").strip('"')
        out.append(Endpoint(cells[0], methods, path, raw=row))
    return out


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

class Report:
    def __init__(self):
        self.errors = 0
        self.warnings = 0

    def error(self, msg):
        self.errors += 1
        print(f"  ERROR   {msg}")

    def warn(self, msg):
        self.warnings += 1
        print(f"  WARNING {msg}")


def check_table(model, lines, rep, draft_name):
    print(f"== Derived Endpoint Reference table ({draft_name})")
    start, end = find_table(lines)
    current = parse_table(lines[start:end])
    derived = {ep.key(): ep for ep in model.endpoints}
    present = {ep.key(): ep for ep in current}
    for ep in current:
        if ep.key() not in derived:
            rep.error(f"line {start + 3 + current.index(ep)}: not derivable from the "
                      f"data objects: {ep.label} | {'/'.join(ep.methods)} {ep.path}")
    for ep in model.endpoints:
        if ep.key() not in present:
            rep.error(f"missing: {ep.label} | {'/'.join(ep.methods)} {ep.path}")
            continue
        cur = present[ep.key()]
        if cur.label != ep.label:
            rep.warn(f"label {cur.label!r} differs from derived {ep.label!r} "
                     f"({'/'.join(ep.methods)} {ep.path})")
    matched = match_rows(model.endpoints, current)
    for w in order_warnings(model.endpoints, [matched[j] for j in sorted(matched)]):
        rep.warn(w)
    for w in placeholder_warnings(model.endpoints, current, matched):
        rep.warn(w)
    if not rep.errors and not rep.warnings:
        print("  table matches the derived endpoint set")


def template_regex(path):
    pat = re.escape(path)
    pat = pat.replace(re.escape("/" + PROCESS_INSTANCE), "/[^/?]+")   # latest | {process-id}
    pat = re.sub(r"\\\{[^}]+\\\}", "[^/?]+", pat)
    return re.compile(pat + "/?$")


class Matcher:
    """Expands process endpoint templates per owner container."""

    def __init__(self, model):
        self.model = model
        self.items = []    # (regex, endpoint, owner or None)
        for ep in model.endpoints:
            if ep.path.startswith("/{collection}/{id}/processes"):
                for container, owner in model.process_owners.items():
                    path = ep.path.replace("/{collection}/{id}", container, 1)
                    self.items.append((template_regex(path), ep, owner))
            else:
                self.items.append((template_regex(ep.path), ep, None))

    def match(self, path):
        return [(ep, owner) for rx, ep, owner in self.items if rx.match(path)]


HTTP_FENCES = ("http-message", "http")
REQUEST_RE = re.compile(r"^([A-Z]+) (\S+) HTTP/\d(?:\.\d)?$")
URI_HEADER_RE = re.compile(r"^(Location|Content-Location|Link):\s*(.*)$", re.I)


def example_blocks(lines):
    """Yield (first_line_no, [(line_no, text)], issue) for every http example
    block, with RFC 8792 '\\' line folding undone; issue is the issue URL of
    a TODO directly following the block (blank lines skipped), else None."""
    i = 0
    while i < len(lines):
        m = re.match(r"^```\s*([\w-]*)", lines[i])
        if not m:
            i += 1
            continue
        lang, start = m.group(1), i
        i += 1
        body = []
        while i < len(lines) and not lines[i].startswith("```"):
            body.append((i + 1, lines[i]))
            i += 1
        i += 1
        if lang not in HTTP_FENCES:
            continue
        j = i
        while j < len(lines) and not lines[j].strip():
            j += 1
        todo = TODO_ISSUE_RE.match(lines[j]) if j < len(lines) else None
        issue = todo.group(1) if todo else None
        unfolded = []
        for no, text in body:
            if unfolded and unfolded[-1][1].endswith("\\"):
                unfolded[-1] = (unfolded[-1][0], unfolded[-1][1][:-1] + text.lstrip())
            else:
                unfolded.append((no, text))
        yield start + 1, unfolded, issue


def check_examples(model, lines, rep, base_path, strip_prefixes, allow_params):
    print("== Paths in http-message examples")
    matcher = Matcher(model)
    collections = {ep.path.split("/")[1] for ep in model.endpoints} - {"{collection}"}
    count = 0
    used = set()    # (endpoint label, parameter) used in an example request
    issue = None    # TODO issue following the current block: errors become warnings

    def error(msg):
        if issue:
            rep.warn(f"{msg} (known issue: {issue})")
        else:
            rep.error(msg)

    def check(no, method, target, absolute_ok):
        nonlocal count
        count += 1
        where = f"line {no}: {method or 'URI'} {target}"
        parts = urlsplit(target)
        path, query = parts.path, parts.query
        if parts.scheme or parts.netloc:
            if not absolute_ok:
                error(f"{where}: absolute-form request target")
                return
            if base_path and not (path == base_path or path.startswith(base_path + "/")):
                error(f"{where}: does not start with the base path {base_path!r}")
                return
            path = path[len(base_path):] if base_path else path
        elif not path.startswith("/"):
            error(f"{where}: path is not absolute (missing leading '/')")
            return
        for prefix in strip_prefixes:
            if path.startswith(prefix + "/"):
                path = path[len(prefix):]
                break

        hits = matcher.match(path)
        if not hits:
            seg = path.split("/")[1] if "/" in path else path
            hint = ""
            if seg not in collections and not path.startswith("/.well-known/"):
                hint = f" (unknown collection segment {seg!r}; known: {', '.join(sorted(collections))})"
            error(f"{where}: no endpoint derived for this path{hint}")
            return
        if method:
            want = {method, "GET"} if method == "HEAD" else {method}
            ok = [(ep, o) for ep, o in hits if want & set(ep.methods)]
            if not ok:
                allowed = sorted({m for ep, _ in hits for m in ep.methods})
                error(f"{where}: method {method} not derived for this path "
                          f"(allowed: {', '.join(allowed)})")
                return
            hits = ok
        for ep, owner in hits:
            if owner is not None and ep.process:
                if ep.process not in model.supported_process_types(owner):
                    rep.warn(f"{where}: {owner['name']} does not declare support for "
                             f"{model.process_types[ep.process]['name']}")
                break
        if query and method:
            known = set().union(*(ep.params for ep, _ in hits)) | allow_params
            for name, _ in parse_qsl(query, keep_blank_values=True):
                used.update((ep.label, name) for ep, _ in hits if name in ep.params)
                if name not in known:
                    rep.warn(f"{where}: query parameter {name!r} is not an operation "
                             f"parameter of {', '.join(sorted({ep.label for ep, _ in hits}))}")

    for _, block, issue in example_blocks(lines):
        for no, text in block:
            m = REQUEST_RE.match(text)
            if m:
                check(no, m.group(1), m.group(2), absolute_ok=False)
                continue
            h = URI_HEADER_RE.match(text)
            if not h:
                continue
            if h.group(1).lower() == "link":
                for uri in re.findall(r"<([^>]+)>", h.group(2)):
                    check(no, None, uri, absolute_ok=True)
            else:
                check(no, None, h.group(2).strip(), absolute_ok=True)
    print(f"  {count} path(s) checked")

    print("== Operation parameters used in examples")
    for ep in model.endpoints:
        for name in sorted(ep.params):
            if (ep.label, name) not in used:
                rep.error(f"{ep.label}: no example request uses operation parameter {name!r}")
    print(f"  {sum(len(ep.params) for ep in model.endpoints)} parameter(s) checked")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--draft", type=Path, default=DEFAULT_DRAFT, help="core draft (mmark source)")
    ap.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="data objects YAML export")
    ap.add_argument("--check", action="append", choices=("table", "examples"),
                    help="check to run (repeatable); default: all")
    ap.add_argument("--print", action="store_true", dest="print_table",
                    help="print the derived table and exit")
    ap.add_argument("--write", action="store_true",
                    help="regenerate the Derived Endpoint Reference table in place")
    ap.add_argument("--base-path", default="/rpp",
                    help="illustrative base URL path stripped from absolute example URIs "
                         "(default: %(default)s; '' for none)")
    ap.add_argument("--strip-prefix", action="append", default=[], metavar="PREFIX",
                    help="additional path prefix to tolerate in examples, e.g. /rpp/v1")
    ap.add_argument("--allow-param", action="append", default=[], metavar="NAME",
                    help="query parameter accepted on every operation (e.g. one defined "
                         "outside the data objects export)")
    ap.add_argument("--irregular", action="append", default=[], metavar="WORD=PLURAL",
                    help="irregular plural defined by a data object specification")
    args = ap.parse_args()

    try:
        data = yaml.safe_load(args.model.read_text(encoding="utf-8"))
        text = args.draft.read_text(encoding="utf-8")
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    irregular = dict(s.split("=", 1) for s in args.irregular)
    model = Model(data, irregular)
    lines = text.split("\n")

    # A model error whose path is named by a TODO with an issue link
    # directly after the table is a known issue: report it as a warning.
    todos = table_todos(lines, find_table(lines)[1])
    errors = []
    for e in model.errors:
        subject = e.split(": ", 1)[0]
        issue = next((iss for _, text, iss in todos if subject in text), None)
        if issue:
            model.warnings.append(f"{e} (known issue: {issue})")
        else:
            errors.append(e)
    model.errors = errors

    for w in model.warnings:
        print(f"model: WARNING {w}", file=sys.stderr)
    for e in model.errors:
        print(f"model: ERROR   {e}", file=sys.stderr)
    status = 1 if model.errors else 0

    if args.print_table or args.write:
        start, end = find_table(lines)
        table, order = render_table(model.endpoints, parse_table(lines[start:end]))
        for w in order:
            print(f"table: WARNING {w}", file=sys.stderr)
    if args.print_table:
        print("\n".join(table))
        return status
    if args.write:
        if lines[start:end] == table:
            print(f"{args.draft.name}: table already up to date")
        else:
            lines[start:end] = table
            args.draft.write_text("\n".join(lines), encoding="utf-8")
            print(f"{args.draft.name}: regenerated table at lines {start + 1}-{start + len(table)}")
        if not args.check:
            return status

    rep = Report()
    checks = args.check or ["table", "examples"]
    if "table" in checks:
        check_table(model, lines, rep, args.draft.name)
    if "examples" in checks:
        check_examples(model, lines, rep, args.base_path.rstrip("/"),
                       [p.rstrip("/") for p in args.strip_prefix], set(args.allow_param))
    print(f"== {rep.errors + len(model.errors)} error(s), "
          f"{rep.warnings + len(model.warnings)} warning(s)")
    return 1 if rep.errors or model.errors else status


if __name__ == "__main__":
    sys.exit(main())
