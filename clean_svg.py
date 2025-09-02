#!/usr/bin/env python3
"""
clean_svg.py -- conservative SVG cleaner that preserves appearance.

Features
- Works on a single file or a whole folder.
- Preserves visual output while removing:
  * <metadata>, comments
  * Unused <defs> children (filters, clips, etc.)
  * Editor/vendor props (inkscape:, sodipodi:, -inkscape-*)
  * Redundant style defaults (with optional --aggressive)
  * Unused element ids

Usage
  # Directory -> Directory (non-recursive)
  python clean_svg.py x --out-dir y

  # Directory -> Directory (recursive; preserves subfolders)
  python clean_svg.py x --out-dir y --recursive

  # Single file -> single file
  python clean_svg.py in.svg -o out.svg

  # Single file -> into an output directory
  python clean_svg.py in.svg --out-dir y

Requires: lxml  (pip install lxml)
"""

import argparse
import re
import sys
from pathlib import Path

try:
    from lxml import etree as ET
except ImportError:
    sys.stderr.write("This script requires lxml. Install with: pip install lxml\n")
    raise

# --- constants ---------------------------------------------------------------

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

URL_REF_RE = re.compile(r"url\(#([^)]+)\)")
HASH_REF_RE = re.compile(r"^#([A-Za-z_][\w.-]*)$")

EDITOR_PROP_PREFIXES = ("-inkscape-",)
STRIP_ATTR_PREFIXES = (
    "{http://www.inkscape.org/namespaces/inkscape}",
    "{http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd}",
)
STRIP_ATTR_QNAMES = ("xml:space",)

CSS_DEFAULTS = {
    "opacity": "1",
    "fill-opacity": "1",
    "stroke-opacity": "1",
    "stroke-dasharray": "none",
    "stroke-linecap": "butt",
    "stroke-linejoin": "miter",
    "stroke-miterlimit": "4",
    # NOTE: default 'stroke' is 'none' -- we do NOT remove fill/stroke values by default.
}

FONT_KEYS = (
    "font",
    "font-family",
    "font-weight",
    "font-size",
    "font-style",
    "font-variant",
    "line-height",
    "letter-spacing",
    "word-spacing",
    "text-anchor",
    "text-decoration",
)

URL_RELEVANT_ATTRS = {
    "filter",
    "clip-path",
    "mask",
    "fill",
    "stroke",
    "marker-start",
    "marker-mid",
    "marker-end",
    "href",
    "{http://www.w3.org/1999/xlink}href",
    "xlink:href",
}

# --- helpers -----------------------------------------------------------------

def localname(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag

def is_textish(elem) -> bool:
    return localname(elem.tag) in ("text", "tspan", "textPath")

def parse_style(style_str: str) -> dict:
    out = {}
    for chunk in style_str.split(";"):
        if not chunk.strip() or ":" not in chunk:
            continue
        k, v = chunk.split(":", 1)
        out[k.strip()] = v.strip()
    return out

def serialize_style(d: dict) -> str:
    # Stable ordering for readability
    order = [
        "filter", "opacity",
        "fill", "fill-opacity",
        "stroke", "stroke-opacity", "stroke-width",
        "stroke-linecap", "stroke-linejoin", "stroke-miterlimit",
        "stroke-dasharray",
    ]
    keys = [k for k in order if k in d] + [k for k in d.keys() if k not in order]
    return ";".join(f"{k}:{d[k]}" for k in keys)

def collect_url_refs_from_value(val: str, out: set):
    if not isinstance(val, str):
        return
    for m in URL_REF_RE.finditer(val):
        out.add(m.group(1))
    m = HASH_REF_RE.match(val.strip())
    if m:
        out.add(m.group(1))

def collect_used_ids(root) -> set:
    """Find ids referenced via url(#id) or #id in attributes/styles."""
    used = set()
    for elem in root.iter():
        for _, val in elem.attrib.items():
            collect_url_refs_from_value(val, used)
        style = elem.get("style")
        if style:
            for _, v in parse_style(style).items():
                collect_url_refs_from_value(v, used)
    return used

# --- cleaning passes ----------------------------------------------------------

def remove_metadata_and_comments(root):
    # Drop <metadata> blocks
    for child in list(root):
        if localname(child.tag) == "metadata":
            root.remove(child)
    # Drop comments anywhere
    for el in root.xpath("//comment()"):
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)

def prune_unused_defs(root, used_ids: set):
    # Keep only <defs> children whose id is referenced
    for defs in root.findall(f".//{{{SVG_NS}}}defs"):
        changed = False
        for child in list(defs):
            cid = child.get("id")
            if not cid or cid not in used_ids:
                defs.remove(child)
                changed = True
        if changed and len(defs) == 0:
            parent = defs.getparent()
            if parent is not None:
                parent.remove(defs)

def strip_editor_attrs(elem):
    to_delete = []
    for attr in elem.attrib:
        if attr in STRIP_ATTR_QNAMES:
            to_delete.append(attr)
            continue
        if any(attr.startswith(p) for p in STRIP_ATTR_PREFIXES):
            to_delete.append(attr)
            continue
        if localname(attr).startswith("data-"):
            to_delete.append(attr)
    for a in to_delete:
        elem.attrib.pop(a, None)

def clean_style(elem, aggressive: bool):
    style = elem.get("style")
    if not style:
        return
    d = parse_style(style)

    # Remove editor/vendor props
    for k in list(d.keys()):
        if any(k.startswith(p) for p in EDITOR_PROP_PREFIXES):
            d.pop(k, None)

    # Remove font props from non-text elements
    if not is_textish(elem):
        for fk in FONT_KEYS:
            d.pop(fk, None)

    # If stroke is none/transparent (or aggressive and undefined), drop stroke-* details
    stroke_val = d.get("stroke", None)
    if (stroke_val is None and aggressive) or (stroke_val and stroke_val.strip() in ("none", "transparent")):
        for k in list(d.keys()):
            if k.startswith("stroke-"):
                d.pop(k, None)
        if aggressive and stroke_val and stroke_val.strip() == "none":
            d.pop("stroke", None)

    # Remove safe defaults
    for k, default in CSS_DEFAULTS.items():
        if d.get(k) == default:
            d.pop(k, None)

    # Redundant fill-opacity:1
    if d.get("fill-opacity") == "1":
        d.pop("fill-opacity", None)

    if d:
        elem.set("style", serialize_style(d))
    else:
        elem.attrib.pop("style", None)

def strip_unused_ids(root, used_ids: set):
    for elem in root.iter():
        eid = elem.get("id")
        if eid and eid not in used_ids and localname(elem.tag) != "svg":
            elem.attrib.pop("id", None)

def normalize_root(root):
    # Ensure root is in the SVG namespace (but DON'T set xmlns manually).
    if localname(root.tag) == "svg":
        if not (isinstance(root.tag, str) and root.tag.startswith("{")):
            root.tag = f"{{{SVG_NS}}}svg"
        else:
            ns = root.tag.split("}", 1)[0][1:]
            if ns != SVG_NS:
                root.tag = f"{{{SVG_NS}}}svg"

    # Register non-empty prefix; cleanup will keep/remove as needed.
    ET.register_namespace("xlink", XLINK_NS)

    # Let lxml calculate the correct namespace declarations.
    ET.cleanup_namespaces(root)

def clean_svg_tree(tree, aggressive: bool):
    root = tree.getroot()
    normalize_root(root)
    remove_metadata_and_comments(root)
    used_ids = collect_used_ids(root)
    prune_unused_defs(root, used_ids)
    for elem in root.iter():
        strip_editor_attrs(elem)
        clean_style(elem, aggressive=aggressive)
    strip_unused_ids(root, used_ids)
    # Final namespace cleanup (idempotent)
    ET.cleanup_namespaces(root)
    return tree

# --- I/O ----------------------------------------------------------------------

def process_file(in_path: Path, out_path: Path, aggressive: bool):
    parser = ET.XMLParser(remove_blank_text=True, recover=True)
    tree = ET.parse(str(in_path), parser)
    tree = clean_svg_tree(tree, aggressive=aggressive)
    xml_bytes = ET.tostring(
        tree,
        xml_declaration=True,
        encoding="utf-8",
        pretty_print=True
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(xml_bytes)

def main():
    ap = argparse.ArgumentParser(description="Clean SVG(s) while preserving appearance.")
    ap.add_argument("input", help="Input SVG file or directory")
    ap.add_argument("-o", "--output", help="Output SVG file (for single-file input)")
    ap.add_argument("--out-dir", help="Output directory (for directory input, or to place a single cleaned file)")
    ap.add_argument("--recursive", action="store_true", help="Recurse into subdirectories when input is a directory")
    ap.add_argument("--aggressive", action="store_true", help="Remove more defaults (still preserves appearance)")
    args = ap.parse_args()

    inp = Path(args.input)
    if not inp.exists():
        sys.stderr.write(f"Input not found: {inp}\n")
        sys.exit(1)

    # Directory mode
    if inp.is_dir():
        out_base = Path(args.out_dir) if args.out_dir else inp.with_name(inp.name + "_cleaned")
        svg_iter = inp.rglob("*.svg") if args.recursive else inp.glob("*.svg")
        count = 0
        for src in svg_iter:
            rel = src.relative_to(inp) if args.recursive else Path(src.name)
            dst = out_base / rel
            process_file(src, dst, aggressive=args.aggressive)
            count += 1
        sys.stderr.write(f"Processed {count} file(s) into {out_base}\n")
        return

    # Single-file mode
    if args.out_dir:
        out_path = Path(args.out_dir) / inp.name
    elif args.output:
        out_path = Path(args.output)
    else:
        out_path = inp.with_suffix(".clean.svg")

    process_file(inp, out_path, aggressive=args.aggressive)
    sys.stderr.write(f"Wrote {out_path}\n")

if __name__ == "__main__":
    main()
