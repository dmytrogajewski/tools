# 04 · A worked example (library function)

One function, start to finish: `CRegMgr::SetSubKey` from `RegMgr.lib`. It's
64 bytes, has one call and one interesting idiom — a good first target.

## 1 · Extract the shipped library and pick the function

The retail library comes from the *NOLF Source Code v1.003* release, mounted
into `make shell` as [02-the-loop](02-the-loop.md#the-library-loop-symbols-available)
describes (`$LIBS` is its `LT2/lithshared/libs/Release`). `dumpfn.py` needs `objdump` (binutils).

```sh
python3 tools/libextract.py $LIBS/RegMgr.lib out/shipped/regmgr
python3 tools/dumpfn.py out/shipped/regmgr/Regmgr.obj "?SetSubKey@"
```

```asm
===== ?SetSubKey@CRegMgr@@QAEHPBD@Z (section 7, 64 bytes, 1 relocs)
   0:  cmpl   $0x0,(%ecx)          ; this->[+0] == 0 ?
   3:  jne    0xa
   5:  xor    %eax,%eax            ; return 0
   7:  ret    $0x4
   a:  mov    0x4(%esp),%eax       ; eax = arg1  (sSubKey)
   e:  test   %eax,%eax
  10:  jne    0x20
  12:  mov    0x14(%ecx),%eax      ; eax = this->[+0x14]
  15:  mov    %eax,0x18(%ecx)      ;       this->[+0x18] = eax
  18:  mov    $0x1,%eax            ; return 1
  1d:  ret    $0x4
  20:  lea    0x18(%ecx),%edx      ; &this->[+0x18]
  23:  push   %edx
  24:  push   %eax                 ; sSubKey
  25:  mov    0x14(%ecx),%eax
  28:  push   %eax                 ; this->[+0x14]
  29:  call   ?CreateKey@CRegMgr@@...   ; CreateKey(+0x14, sSubKey, &+0x18)
  2e:  neg    %eax                 ;\
  30:  sbb    %eax,%eax            ; } !!eax  → 0 or 1
  32:  neg    %eax                 ;/
  34:  ret    $0x4
```

## 2 · Read it back into C++

Decode top to bottom:

- `__thiscall` (args after `this` in `ecx`), returns `int`, one pointer arg.
  The mangled name `?SetSubKey@CRegMgr@@QAEHPBD@Z` confirms: `CRegMgr::SetSubKey(char const*)` returning `int`.
- `this->[+0] == 0 → return 0`. Offset 0 is the first member. From the class
  that's `m_bInitialized`, so: `if (!m_bInitialized) return FALSE;`
- `if (arg == 0) { this->[+0x18] = this->[+0x14]; return 1; }`. Offsets +0x14
  and +0x18 are `m_hVersionKey` and `m_hSubKey`:
  `if (sSubKey == NULL) { m_hSubKey = m_hVersionKey; return TRUE; }`
- else `CreateKey(m_hVersionKey, sSubKey, m_hSubKey)`, and the `neg;sbb;neg`
  is the `!!` idiom: the result normalised to 0 or 1. That is what
  `if (CreateKey(...)) return TRUE; else return FALSE;` compiles to.

Matching the source we'd write:

```cpp
BOOL CRegMgr::SetSubKey(const char* sSubKey) {
  if (!m_bInitialized) return FALSE;
  if (sSubKey == NULL) {
    m_hSubKey = m_hVersionKey;
    return TRUE;
  }
  else {
    if (CreateKey(m_hVersionKey, sSubKey, m_hSubKey)) return TRUE;
    else return FALSE;
  }
}
```

The member offsets (+0, +0x14, +0x18) must line up with the class layout in the
header (`src/LT2/lithshared/incs/regmgr.h`). If your struct puts a member at the
wrong offset, *every* access in *every* method is wrong — layout is the first
thing to get right, and matched methods prove it.

## 3 · Compile just this TU

In `make shell` (the libraries are built with cl 8447, as the retail ones
were -- `build/libs.sh`):

```sh
cd /repo/src/lithshared/RegMgr
CL_REV=8447 source /repo/build/env.sh
rm -f Release/regmgr.obj
run_cl /nologo /MT /W3 /GX /O2 /D WIN32 /D NDEBUG /D _WINDOWS /D NO_PRAGMA_LIBS \
    /I ..\\..\\LT2\\lithshared\\incs /I . /c /FoRelease/ regmgr.cpp
```

(`build/libs.sh regmgr` does the same and rebuilds `src/lithshared/out/RegMgr.lib`.)

## 4 · Diff

```sh
python3 tools/objdiff.py src/lithshared/RegMgr/Release/regmgr.obj out/shipped/regmgr/Regmgr.obj
```

```
functions: ours 14, theirs 14, common 14
  identical : 14/14 (100.0%)
```

Done — `SetSubKey` is byte-identical (and the rest of RegMgr with it).

## If it had come back "differing"

Say the diff reported `?SetSubKey@... (66 vs 64 bytes)`. You'd:

```sh
python3 tools/dumpfn.py src/lithshared/RegMgr/Release/regmgr.obj "?SetSubKey@" > out/ours.txt
python3 tools/dumpfn.py out/shipped/regmgr/Regmgr.obj "?SetSubKey@" > out/theirs.txt
diff out/ours.txt out/theirs.txt
```

Two extra bytes on a bool return usually means you wrote `return CreateKey(...)`
(a direct return of the call's value, no normalization) where shipped wrote the
`if/else return TRUE/FALSE` form, or vice versa — see the boolean idioms in
[03-reading-disassembly](03-reading-disassembly.md#comparisons-and-booleans).
Change the source, recompile, re-diff. Each round changes exactly what you
predicted, or you misread the idiom — never shotgun edits.

## The takeaway

Every function is this loop. The skill is step 2 — reading the codegen back to
source — and that's just fluency with [03-reading-disassembly](03-reading-disassembly.md),
built one function at a time.
