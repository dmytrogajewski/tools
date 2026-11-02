#!/usr/bin/env python3
"""Write a PE32 binary's resources back out as resource-script source.

Decompiles the resource types an MFC tool of the VC6 era carries (DIALOG
and DIALOGEX, STRINGTABLE, VERSIONINFO, ICON, CURSOR, BITMAP, MENU,
ACCELERATORS) into an .rc that compiles back to the same payloads, and
writes the binary ones (icons, cursors, bitmaps, unknown types) to files
beside it. Check the round trip with rsrcdiff.py: the aim is every
resource byte-identical.

Usage: rsrc2rc.py <image> <out.rc> [--resource-h=<resource.h>] [--res-dir=res]
                  [--name=<stem for the binary files>]

--resource-h reads `#define NAME value` lines and uses the names for
resource and control ids (an id the header doesn't name stays a number).

Control statements are written in their short forms (LTEXT, PUSHBUTTON,
EDITTEXT, ...) when the style is one rc.exe would build from the short form
with OR'd / NOT'd bits, else as CONTROL; either way the item compiles to
the shipped bytes. Style bits are written by name where they have one.
"""
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rsrcdiff  # noqa: E402

RT = {1: "CURSOR", 2: "BITMAP", 3: "ICON", 4: "MENU", 5: "DIALOG", 6: "STRING",
      9: "ACCELERATORS", 12: "GROUP_CURSOR", 14: "GROUP_ICON", 16: "VERSION"}

WS = [(0x80000000, "WS_POPUP"), (0x40000000, "WS_CHILD"), (0x20000000, "WS_MINIMIZE"),
      (0x10000000, "WS_VISIBLE"), (0x08000000, "WS_DISABLED"), (0x04000000, "WS_CLIPSIBLINGS"),
      (0x02000000, "WS_CLIPCHILDREN"), (0x01000000, "WS_MAXIMIZE"), (0x00C00000, "WS_CAPTION"),
      (0x00800000, "WS_BORDER"), (0x00400000, "WS_DLGFRAME"), (0x00200000, "WS_VSCROLL"),
      (0x00100000, "WS_HSCROLL"), (0x00080000, "WS_SYSMENU"), (0x00040000, "WS_THICKFRAME")]
# 0x20000 / 0x10000 are WS_GROUP / WS_TABSTOP on a control, WS_MINIMIZEBOX /
# WS_MAXIMIZEBOX on a window
WS_CTL = [(0x00020000, "WS_GROUP"), (0x00010000, "WS_TABSTOP")]
WS_WND = [(0x00020000, "WS_MINIMIZEBOX"), (0x00010000, "WS_MAXIMIZEBOX")]
DS = [(0x0001, "DS_ABSALIGN"), (0x0002, "DS_SYSMODAL"), (0x0004, "DS_3DLOOK"), (0x0008, "DS_FIXEDSYS"),
      (0x0010, "DS_NOFAILCREATE"), (0x0020, "DS_LOCALEDIT"), (0x0040, "DS_SETFONT"),
      (0x0080, "DS_MODALFRAME"), (0x0100, "DS_NOIDLEMSG"), (0x0200, "DS_SETFOREGROUND"),
      (0x0400, "DS_CONTROL"), (0x0800, "DS_CENTER"), (0x1000, "DS_CENTERMOUSE"),
      (0x2000, "DS_CONTEXTHELP")]
WS_EX = [(0x00000001, "WS_EX_DLGMODALFRAME"), (0x00000004, "WS_EX_NOPARENTNOTIFY"),
         (0x00000008, "WS_EX_TOPMOST"), (0x00000010, "WS_EX_ACCEPTFILES"),
         (0x00000020, "WS_EX_TRANSPARENT"), (0x00000040, "WS_EX_MDICHILD"),
         (0x00000080, "WS_EX_TOOLWINDOW"), (0x00000100, "WS_EX_WINDOWEDGE"),
         (0x00000200, "WS_EX_CLIENTEDGE"), (0x00000400, "WS_EX_CONTEXTHELP"),
         (0x00001000, "WS_EX_RIGHT"), (0x00002000, "WS_EX_RTLREADING"),
         (0x00004000, "WS_EX_LEFTSCROLLBAR"), (0x00010000, "WS_EX_CONTROLPARENT"),
         (0x00020000, "WS_EX_STATICEDGE"), (0x00040000, "WS_EX_APPWINDOW")]
# the low word's names by class (atoms 0x80..0x85 and the common controls)
CLS_BITS = {
    "BUTTON": [(0x0F, {0: "BS_PUSHBUTTON", 1: "BS_DEFPUSHBUTTON", 2: "BS_CHECKBOX", 3: "BS_AUTOCHECKBOX",
                       4: "BS_RADIOBUTTON", 5: "BS_3STATE", 6: "BS_AUTO3STATE", 7: "BS_GROUPBOX",
                       8: "BS_USERBUTTON", 9: "BS_AUTORADIOBUTTON", 11: "BS_OWNERDRAW"}),
               (0x20, "BS_LEFTTEXT"), (0x40, "BS_ICON"), (0x80, "BS_BITMAP"),
               (0x300, {0x100: "BS_LEFT", 0x200: "BS_RIGHT", 0x300: "BS_CENTER"}),
               (0xC00, {0x400: "BS_TOP", 0x800: "BS_BOTTOM", 0xC00: "BS_VCENTER"}),
               (0x1000, "BS_PUSHLIKE"), (0x2000, "BS_MULTILINE"), (0x4000, "BS_NOTIFY"),
               (0x8000, "BS_FLAT")],
    "EDIT": [(0x3, {0: "ES_LEFT", 1: "ES_CENTER", 2: "ES_RIGHT"}), (0x4, "ES_MULTILINE"),
             (0x8, "ES_UPPERCASE"), (0x10, "ES_LOWERCASE"), (0x20, "ES_PASSWORD"),
             (0x40, "ES_AUTOVSCROLL"), (0x80, "ES_AUTOHSCROLL"), (0x100, "ES_NOHIDESEL"),
             (0x400, "ES_OEMCONVERT"), (0x800, "ES_READONLY"), (0x1000, "ES_WANTRETURN"),
             (0x2000, "ES_NUMBER")],
    "STATIC": [(0x1F, {0: "SS_LEFT", 1: "SS_CENTER", 2: "SS_RIGHT", 3: "SS_ICON", 4: "SS_BLACKRECT",
                       5: "SS_GRAYRECT", 6: "SS_WHITERECT", 7: "SS_BLACKFRAME", 8: "SS_GRAYFRAME",
                       9: "SS_WHITEFRAME", 11: "SS_SIMPLE", 12: "SS_LEFTNOWORDWRAP",
                       13: "SS_OWNERDRAW", 14: "SS_BITMAP", 15: "SS_ENHMETAFILE",
                       16: "SS_ETCHEDHORZ", 17: "SS_ETCHEDVERT", 18: "SS_ETCHEDFRAME"}),
               (0x40, "SS_REALSIZEIMAGE"), (0x80, "SS_NOPREFIX"), (0x100, "SS_NOTIFY"),
               (0x200, "SS_CENTERIMAGE"), (0x400, "SS_RIGHTJUST"), (0x1000, "SS_SUNKEN")],
    "LISTBOX": [(0x1, "LBS_NOTIFY"), (0x2, "LBS_SORT"), (0x4, "LBS_NOREDRAW"), (0x8, "LBS_MULTIPLESEL"),
                (0x10, "LBS_OWNERDRAWFIXED"), (0x20, "LBS_OWNERDRAWVARIABLE"), (0x40, "LBS_HASSTRINGS"),
                (0x80, "LBS_USETABSTOPS"), (0x100, "LBS_NOINTEGRALHEIGHT"), (0x200, "LBS_MULTICOLUMN"),
                (0x400, "LBS_WANTKEYBOARDINPUT"), (0x800, "LBS_EXTENDEDSEL"),
                (0x1000, "LBS_DISABLENOSCROLL"), (0x2000, "LBS_NODATA"), (0x4000, "LBS_NOSEL")],
    "COMBOBOX": [(0x3, {1: "CBS_SIMPLE", 2: "CBS_DROPDOWN", 3: "CBS_DROPDOWNLIST"}),
                 (0x10, "CBS_OWNERDRAWFIXED"), (0x20, "CBS_OWNERDRAWVARIABLE"), (0x40, "CBS_AUTOHSCROLL"),
                 (0x80, "CBS_OEMCONVERT"), (0x100, "CBS_SORT"), (0x200, "CBS_HASSTRINGS"),
                 (0x400, "CBS_NOINTEGRALHEIGHT"), (0x800, "CBS_DISABLENOSCROLL"),
                 (0x2000, "CBS_UPPERCASE"), (0x4000, "CBS_LOWERCASE")],
    "SCROLLBAR": [(0x1, "SBS_VERT")],
    "msctls_trackbar32": [(0x1, "TBS_AUTOTICKS"), (0x2, {0x2: "TBS_VERT"}), (0x4, "TBS_LEFT"),
                          (0x8, "TBS_BOTH"), (0x10, "TBS_NOTICKS"), (0x20, "TBS_ENABLESELRANGE"),
                          (0x40, "TBS_FIXEDLENGTH"), (0x80, "TBS_NOTHUMB"), (0x100, "TBS_TOOLTIPS")],
    "msctls_progress32": [(0x1, "PBS_SMOOTH"), (0x4, "PBS_VERTICAL")],
    "msctls_updown32": [(0x1, "UDS_WRAP"), (0x2, "UDS_SETBUDDYINT"), (0x4, "UDS_ALIGNRIGHT"),
                        (0x8, "UDS_ALIGNLEFT"), (0x10, "UDS_AUTOBUDDY"), (0x20, "UDS_ARROWKEYS"),
                        (0x40, "UDS_HORZ"), (0x80, "UDS_NOTHOUSANDS"), (0x100, "UDS_HOTTRACK")],
}
ATOM = {0x80: "BUTTON", 0x81: "EDIT", 0x82: "STATIC", 0x83: "LISTBOX", 0x84: "SCROLLBAR", 0x85: "COMBOBOX"}
CLASS_NAME = {"BUTTON": "Button", "EDIT": "Edit", "STATIC": "Static", "LISTBOX": "ListBox",
              "SCROLLBAR": "ScrollBar", "COMBOBOX": "ComboBox"}
CHILD_VIS = 0x50000000

# The short forms: (statement, class, its default style before WS_CHILD |
# WS_VISIBLE, the low-word "kind" it fixes, has text). rc.exe builds
# WS_CHILD | WS_VISIBLE | default | <given style>, and NOT clears bits.
SHORT = [
    ("LTEXT", "STATIC", 0x00020000 | 0x0, 0x1F, 0x0, True),
    ("CTEXT", "STATIC", 0x00020000 | 0x1, 0x1F, 0x1, True),
    ("RTEXT", "STATIC", 0x00020000 | 0x2, 0x1F, 0x2, True),
    ("ICON", "STATIC", 0x3, 0x1F, 0x3, True),
    ("DEFPUSHBUTTON", "BUTTON", 0x00010000 | 0x1, 0x0F, 0x1, True),
    ("PUSHBUTTON", "BUTTON", 0x00010000 | 0x0, 0x0F, 0x0, True),
    ("CHECKBOX", "BUTTON", 0x00010000 | 0x2, 0x0F, 0x2, True),
    ("AUTOCHECKBOX", "BUTTON", 0x00010000 | 0x3, 0x0F, 0x3, True),
    ("RADIOBUTTON", "BUTTON", 0x4, 0x0F, 0x4, True),
    ("AUTORADIOBUTTON", "BUTTON", 0x9, 0x0F, 0x9, True),
    ("STATE3", "BUTTON", 0x00010000 | 0x5, 0x0F, 0x5, True),
    ("AUTO3STATE", "BUTTON", 0x00010000 | 0x6, 0x0F, 0x6, True),
    ("GROUPBOX", "BUTTON", 0x7, 0x0F, 0x7, True),
    ("EDITTEXT", "EDIT", 0x00810000, 0, 0, False),
    ("LISTBOX", "LISTBOX", 0x00800001, 0, 0, False),
    ("COMBOBOX", "COMBOBOX", 0x00010000, 0, 0, False),
    ("SCROLLBAR", "SCROLLBAR", 0x0, 0, 0, False),
]


def names(bits, table, cls=None):
    """Bits as `A | B | 0x..`, by the tables (a table entry is (bit, name) or
    (mask, {value: name}))."""
    out, left = [], bits
    for bit, nm in table:
        if isinstance(nm, dict):
            v = bits & bit
            if v in nm and (v or nm[v] not in ("BS_PUSHBUTTON", "ES_LEFT", "SS_LEFT")):
                out.append(nm[v])
                left &= ~bit
            elif v in nm:
                left &= ~bit
        elif bits & bit == bit and bit:
            out.append(nm)
            left &= ~bit
    if left:
        out.append("0x%x" % left)
    return out


def style_names(style, cls, window):
    hi = WS + (WS_WND if window else WS_CTL)
    t = list(hi)
    if window:
        t += DS
    elif cls in CLS_BITS:
        t += CLS_BITS[cls]
    # WS_CAPTION is WS_BORDER | WS_DLGFRAME: name the pair once
    out = names(style, t, cls)
    if "WS_CAPTION" in out:
        out = [n for n in out if n not in ("WS_BORDER", "WS_DLGFRAME")]
    return out


def rcstr(s):
    s = s.replace('"', '""').replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    s = s.replace("\0", "\\0")
    return '"' + "".join(c if 32 <= ord(c) < 127 or _cp1252(c) else "\\x%04x" % ord(c) for c in s) + '"'


def _cp1252(c):
    """A character the .rc (written in code page 1252) can hold as itself."""
    try:
        return ord(c) >= 0xA0 and len(c.encode("cp1252")) == 1
    except UnicodeEncodeError:
        return False


class Reader:
    def __init__(self, b, o=0):
        self.b, self.o = b, o

    def u16(self):
        v = struct.unpack_from("<H", self.b, self.o)[0]
        self.o += 2
        return v

    def i16(self):
        v = struct.unpack_from("<h", self.b, self.o)[0]
        self.o += 2
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.b, self.o)[0]
        self.o += 4
        return v

    def sz(self):
        e = self.o
        while self.b[e:e + 2] != b"\0\0":
            e += 2
        s = self.b[self.o:e].decode("utf-16le")
        self.o = e + 2
        return s

    def sz_or_ord(self):
        if struct.unpack_from("<H", self.b, self.o)[0] == 0xFFFF:
            self.o += 2
            return self.u16()
        return self.sz()

    def align(self, n=4):
        self.o = (self.o + n - 1) & ~(n - 1)


class Ids:
    def __init__(self, path):
        self.by_value = {}
        if path:
            for l in open(path, errors="replace"):
                m = re.match(r"\s*#\s*define\s+(\w+)\s+(0x[0-9a-fA-F]+|\d+)\b", l)
                if m:
                    self.by_value.setdefault(int(m.group(2), 0), []).append(m.group(1))

    def name(self, v, prefixes):
        if v == 0xFFFF or v == -1:
            return "IDC_STATIC"
        for n in self.by_value.get(v, []):
            if n.startswith(prefixes):
                return n
        return str(v)


STD_IDS = {1: "IDOK", 2: "IDCANCEL", 3: "IDABORT", 4: "IDRETRY", 5: "IDIGNORE", 6: "IDYES", 7: "IDNO"}


def rname(nm, ids, prefixes):
    return ids.name(nm, prefixes) if isinstance(nm, int) else nm


def dialog(nm, data, ids):
    r = Reader(data)
    ex = data[0:4] == b"\x01\x00\xff\xff"
    if ex:
        r.o = 4
        _help, exstyle, style = r.u32(), r.u32(), r.u32()
    else:
        style, exstyle = r.u32(), r.u32()
    n = r.u16()
    x, y, cx, cy = r.i16(), r.i16(), r.i16(), r.i16()
    menu, cls, cap = r.sz_or_ord(), r.sz_or_ord(), r.sz()
    font = None
    if style & 0x40:
        pt = r.u16()
        weight = italic = charset = None
        if ex:
            weight, italic, charset = r.u16(), r.b[r.o], r.b[r.o + 1]
            r.o += 2
        font = (pt, r.sz(), weight, italic, charset)
    L = ["%s %s %d, %d, %d, %d" % (rname(nm, ids, ("IDD_",)), "DIALOGEX" if ex else "DIALOG", x, y, cx, cy)]
    L.append("STYLE " + " | ".join(style_names(style, None, True) or ["0"]))
    if exstyle:
        L.append("EXSTYLE " + " | ".join(names(exstyle, WS_EX)))
    if cap or style & 0x00C00000 == 0x00C00000:
        L.append("CAPTION " + rcstr(cap))
    if menu:
        L.append("MENU " + (rname(menu, ids, ("IDR_", "IDM_")) if isinstance(menu, int) else menu))
    if cls:
        L.append("CLASS " + (rcstr(cls) if isinstance(cls, str) else str(cls)))
    if font:
        pt, face, weight, italic, charset = font
        L.append("FONT %d, %s" % (pt, rcstr(face)) +
                 (", %d, %d, 0x%x" % (weight, italic, charset) if ex else ""))
    L.append("BEGIN")
    for _ in range(n):
        r.align()
        if ex:
            help_, exs, st = r.u32(), r.u32(), r.u32()
        else:
            st, exs = r.u32(), r.u32()
            help_ = 0
        ix, iy, icx, icy = r.i16(), r.i16(), r.i16(), r.i16()
        iid = r.u32() if ex else r.u16()
        icls = r.sz_or_ord()
        text = r.sz_or_ord()
        extra = r.u16()
        r.o += extra
        L.append("    " + control(st, exs, help_, ix, iy, icx, icy, iid, icls, text, ids, ex))
    L.append("END")
    return "\n".join(L)


def control(st, exs, help_, x, y, cx, cy, iid, icls, text, ids, ex):
    cname = ATOM.get(icls, icls) if isinstance(icls, int) else icls
    key = cname.upper() if isinstance(cname, str) else cname
    if isinstance(key, str) and key in ATOM.values():
        cname = key
    idn = STD_IDS.get(iid) or ids.name(-1 if iid in (0xFFFF, 0xFFFFFFFF) else iid, ("IDC_", "ID_", "IDOK", "IDCANCEL"))
    if isinstance(text, int):
        txt = rname(text, ids, ("IDR_", "IDI_", "IDB_"))
    else:
        txt = rcstr(text)
    pos = "%d, %d, %d, %d" % (x, y, cx, cy)
    tail = ""
    if exs or (ex and help_):
        tail = ", " + (" | ".join(names(exs, WS_EX)) if exs else "0")
        if ex and help_:
            tail += ", %d" % help_
    for stmt, c, dflt, kmask, kind, has_text in SHORT:
        if c != cname or (kmask and st & kmask != kind):
            continue
        if stmt == "ICON" and not isinstance(text, int):
            continue
        base = CHILD_VIS | dflt
        add, drop = st & ~base, base & ~st
        if drop & ~0x10030000 & ~0x00800000 & ~0x00200000:
            continue   # NOT only for the visible / group / tabstop / border bits
        parts = style_names(add, cname, False)
        parts += ["NOT " + n for n in style_names(drop, cname, False)]
        sty = " | ".join(parts)
        lead = (txt + ", ") if has_text else ""
        s = "%-15s %s%s, %s" % (stmt, lead, idn, pos)
        if sty or tail:
            s += ", " + (sty or "0")
        return s + tail
    # CONTROL: rc ORs in WS_CHILD | WS_VISIBLE
    base = CHILD_VIS
    parts = style_names(st & ~base, cname, False)
    if not st & 0x10000000:
        parts.append("NOT WS_VISIBLE")
    if not st & 0x40000000:
        parts.append("NOT WS_CHILD")
    cl = rcstr(CLASS_NAME.get(cname, cname)) if isinstance(cname, str) else "0x%x" % cname
    return "CONTROL         %s, %s, %s, %s, %s%s" % (txt, idn, cl, " | ".join(parts) or "0", pos, tail)


def stringtable(block, data, ids):
    r = Reader(data)
    L = ["STRINGTABLE DISCARDABLE", "BEGIN"]
    for i in range(16):
        n = r.u16()
        s = r.b[r.o:r.o + 2 * n].decode("utf-16le")
        r.o += 2 * n
        if n:
            L.append("    %-31s %s" % (ids.name((block - 1) * 16 + i, ("IDS_", "IDR_", "IDP_", "ID_", "AFX_")), rcstr(s)))
    L.append("END")
    return "\n".join(L)


def version(data):
    def node(o, depth):
        ln, vln, typ = struct.unpack_from("<HHH", data, o)
        r = Reader(data, o + 6)
        key = r.sz()
        r.align()
        val = data[r.o:r.o + (vln * 2 if typ == 1 else vln)]
        r.o += len(val)
        r.align()
        kids = []
        while r.o < o + ln:
            k, nxt = node(r.o, depth + 1)
            kids.append(k)
            r.o = (nxt + 3) & ~3
        return (key, typ, val, kids), o + ln

    (key, _t, ffi, kids), _ = node(0, 0)
    f = struct.unpack_from("<13I", ffi)
    ver = lambda hi, lo: "%d,%d,%d,%d" % (hi >> 16, hi & 0xFFFF, lo >> 16, lo & 0xFFFF)
    L = ["VS_VERSION_INFO VERSIONINFO",
         " FILEVERSION " + ver(f[2], f[3]), " PRODUCTVERSION " + ver(f[4], f[5]),
         " FILEFLAGSMASK 0x%xL" % f[6], " FILEFLAGS 0x%xL" % f[7], " FILEOS 0x%xL" % f[8],
         " FILETYPE 0x%xL" % f[9], " FILESUBTYPE 0x%xL" % f[10], "BEGIN"]

    def block(k, ind):
        key, typ, val, kids = k
        pad = "    " * ind
        if kids or (not val and key in ("StringFileInfo", "VarFileInfo")):
            L.append('%sBLOCK "%s"' % (pad, key))
            L.append(pad + "BEGIN")
            for c in kids:
                block(c, ind + 1)
            L.append(pad + "END")
        elif typ == 1:
            s = val.decode("utf-16le")
            L.append('%sVALUE "%s", %s' % (pad, key, rcstr(s)))
        else:
            ws = struct.unpack_from("<%dH" % (len(val) // 2), val)
            L.append('%sVALUE "%s", %s' % (pad, key, ", ".join("0x%x" % w for w in ws)))

    for k in kids:
        block(k, 1)
    L.append("END")
    return "\n".join(L)


def menu(nm, data, ids):
    r = Reader(data, 4)
    L = ["%s MENU DISCARDABLE" % rname(nm, ids, ("IDR_", "IDM_")), "BEGIN"]

    def items(ind):
        while True:
            fl = r.u16()
            pad = "    " * ind
            if fl & 0x10:
                t = r.sz()
                L.append("%sPOPUP %s%s" % (pad, rcstr(t), opts(fl & ~0x90)))
                L.append(pad + "BEGIN")
                items(ind + 1)
                L.append(pad + "END")
            else:
                i, t = r.u16(), r.sz()
                if not t and not i and not fl & ~0x80:
                    L.append(pad + "MENUITEM SEPARATOR")
                else:
                    L.append("%sMENUITEM %s, %s%s" % (pad, rcstr(t), ids.name(i, ("ID_", "IDM_")), opts(fl & ~0x80)))
            if fl & 0x80:
                return

    def opts(fl):
        o = [n for b, n in ((0x8, "CHECKED"), (0x1, "GRAYED"), (0x2, "INACTIVE"), (0x40, "HELP"),
                            (0x20, "MENUBARBREAK"), (0x4, "MENUBREAK")) if fl & b]
        return (", " + ", ".join(o)) if o else ""

    items(1)
    L.append("END")
    return "\n".join(L)


def accelerators(nm, data, ids):
    L = ["%s ACCELERATORS DISCARDABLE" % rname(nm, ids, ("IDR_",)), "BEGIN"]
    for o in range(0, len(data), 8):
        fl, key, cmd, _ = struct.unpack_from("<HHHH", data, o)
        o_ = []
        if fl & 1:
            o_.append("VIRTKEY")
        else:
            o_.append("ASCII")
        o_ += [n for b, n in ((0x2, "NOINVERT"), (0x4, "SHIFT"), (0x8, "CONTROL"), (0x10, "ALT")) if fl & b]
        k = ("%d" % key) if fl & 1 else rcstr(chr(key))
        L.append("    %s, %s, %s" % (k, ids.name(cmd, ("ID_", "IDM_")), ", ".join(o_)))
        if fl & 0x80:
            break
    L.append("END")
    return "\n".join(L)


def group(nm, grp, res, kind, path):
    """A GROUP_ICON / GROUP_CURSOR and its images as one .ico / .cur file."""
    _r, typ, n = struct.unpack_from("<HHH", grp, 0)
    ents, imgs = [], []
    for i in range(n):
        e = grp[6 + 14 * i:6 + 14 * i + 14]
        w, h, c, z, p, b, sz, iid = struct.unpack("<BBBBHHIH", e)
        img = res.get((kind, iid))
        if kind == 1:   # a cursor image starts with its hotspot
            hx, hy = struct.unpack_from("<HH", img, 0)
            img = img[4:]
            p, b = hx, hy
            h //= 2
        ents.append((w, h, c, z, p, b, len(img)))
        imgs.append(img)
    out = bytearray(struct.pack("<HHH", 0, typ, n))
    off = 6 + 16 * n
    for (w, h, c, z, p, b, sz) in ents:
        out += struct.pack("<BBBBHHII", w, h, c, z, p, b, sz, off)
        off += sz
    for img in imgs:
        out += img
    open(path, "wb").write(out)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opt = dict(a[2:].split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    image, out = args
    ids = Ids(opt.get("resource-h"))
    resdir = opt.get("res-dir", "res")
    stem = opt.get("name", os.path.splitext(os.path.basename(out))[0])
    absres = os.path.join(os.path.dirname(os.path.abspath(out)), resdir)
    raw = rsrcdiff.resources(image)
    langs = sorted({k[2] for k in raw})
    if len(langs) != 1:
        sys.exit("rsrc2rc.py: resources in %d languages: one .rc each isn't done" % len(langs))
    lang = langs[0]
    res = {(k[0], k[1]): v for k, v in raw.items()}
    S = {}

    def sect(t, s):
        S.setdefault(t, []).append(s)

    nbin = {}
    for (typ, nm), data in sorted(res.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        if typ in (1, 3):
            continue   # written with their groups
        if typ == 5:
            sect("Dialog", dialog(nm, data, ids))
        elif typ == 6:
            sect("String Table", stringtable(nm, data, ids))
        elif typ == 16:
            sect("Version", version(data))
        elif typ == 4:
            sect("Menu", menu(nm, data, ids))
        elif typ == 9:
            sect("Accelerator", accelerators(nm, data, ids))
        elif typ in (14, 12):
            os.makedirs(absres, exist_ok=True)
            ext = ".ico" if typ == 14 else ".cur"
            n = nbin.get(ext, 0)
            nbin[ext] = n + 1
            fn = "%s%s%s" % (stem, "" if not n else n + 1, ext)
            group(nm, data, res, 3 if typ == 14 else 1, os.path.join(absres, fn))
            sect("Icon" if typ == 14 else "Cursor", '%-23s %s DISCARDABLE     "%s\\\\%s"' % (
                rname(nm, ids, ("IDR_", "IDI_", "IDC_")), "ICON" if typ == 14 else "CURSOR", resdir, fn))
        elif typ == 2:
            os.makedirs(absres, exist_ok=True)
            fn = "%s_%s.bmp" % (stem, nm)
            hdr = struct.unpack_from("<I", data, 0)[0]
            bits, clr = struct.unpack_from("<H", data, 14)[0], struct.unpack_from("<I", data, 32)[0]
            pal = (clr or (1 << bits if bits <= 8 else 0)) * 4
            open(os.path.join(absres, fn), "wb").write(
                b"BM" + struct.pack("<IHHI", 14 + len(data), 0, 0, 14 + hdr + pal) + data)
            sect("Bitmap", '%-23s BITMAP  DISCARDABLE     "%s\\\\%s"' % (rname(nm, ids, ("IDB_", "IDR_")), resdir, fn))
        else:
            os.makedirs(absres, exist_ok=True)
            tn = RT.get(typ, typ if isinstance(typ, str) else "%d" % typ)
            fn = "%s_%s_%s.bin" % (stem, tn, nm)
            open(os.path.join(absres, fn), "wb").write(data)
            sect(str(tn), '%-23s %s DISCARDABLE "%s\\\\%s"' % (nm, rcstr(typ) if isinstance(typ, str) else typ, resdir, fn))

    from_lang = {0x409: ("LANG_ENGLISH", "SUBLANG_ENGLISH_US", 1252),
                 0x809: ("LANG_ENGLISH", "SUBLANG_ENGLISH_UK", 1252)}
    ln, sl, cp = from_lang.get(lang, ("0x%x" % (lang & 0x3FF), "0x%x" % (lang >> 10), 1252))
    rh = os.path.basename(opt["resource-h"]) if "resource-h" in opt else None
    L = ["// Microsoft Visual C++ generated resource script.", "//"]
    if rh:
        L.append('#include "%s"' % rh)
    L += ["", '#define APSTUDIO_READONLY_SYMBOLS', '#include "afxres.h"', '#undef APSTUDIO_READONLY_SYMBOLS', "",
          "#if !defined(AFX_RESOURCE_DLL) || defined(AFX_TARG_ENU)",
          "#ifdef _WIN32", "LANGUAGE %s, %s" % (ln, sl), "#pragma code_page(%d)" % cp, "#endif //_WIN32", ""]
    for t in ("Icon", "Cursor", "Bitmap", "Menu", "Dialog", "Accelerator", "Version", "String Table"):
        for s in S.pop(t, []):
            L += [s, ""]
    for t, ss in S.items():
        for s in ss:
            L += [s, ""]
    L += ["#endif", ""]
    open(out, "w", newline="\r\n", encoding="cp1252").write("\n".join(L))


if __name__ == "__main__":
    main()
