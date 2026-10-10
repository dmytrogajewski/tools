# 08 · A worked example: chasing byte-identity (CServerMgr::PlaySound)

[06-worked-example-engine](06-worked-example-engine.md) reconstructed a 71-byte
argument-checker. This guide is the other end of the difficulty scale: a
494-byte function with multiple inheritance, floating point, an SDK struct, two
inlined list operations, and — the real subject of this guide — a whole
catalogue of VC6 idioms packed into one function. `CServerMgr::PlaySound`
(`0x10034037`, now in `src/engine/server/smgr.cpp`) went from Ghidra
pseudocode to byte-identical in about a dozen compiles, and every wrong
attempt taught a reusable rule. Those rules are the
point; the sound system is just the vehicle.

Prerequisites: the loop from [02](02-the-loop.md), the disassembly conventions
from [03](03-reading-disassembly.md), vtable naming from [07](07-vtables.md).

---

## 1 · What we start with

PlaySound has no `Class::Method` check literal, but it sits in slot 1 of the
5-slot sound-interface vtable at `0x1004e2dc` — CServerMgr's second base.
`iltsoundmgr.h` names the slot and gives the exact signature:

```cpp
virtual LTRESULT PlaySound(PlaySoundInfo* pPlaySoundInfo, HLTSOUND& hResult);
```

The SDK also ships `PlaySoundInfo` itself (`src/LT2/sdk/inc/ltbasedefs.h`), with every field
offset fixed: `m_dwFlags` +0x000, `m_szSoundName` +0x004, `m_hSound` +0x110,
`m_hObject` +0x10c, `m_nVolume` +0x128, `m_fPitchShift` +0x12c,
`m_fOuterRadius`/`m_fInnerRadius` +0x118/+0x11c — and the `PLAYSOUND_*` flag
constants. When the disassembly reads `testb $0x8,(%esi)` you don't guess:
`0x8 = PLAYSOUND_ATTACHED`.

Ghidra's decompile gives the skeleton: null-check the info, check a field at
`this+0x36c`, call something with the filename, clamp two floats, mask some
flags, then either allocate-init-link an object or memcpy the whole struct
somewhere. Good enough to start; wrong in every detail that matters.

---

## 2 · `this` is not where you think: the second base

The first thing to settle in any CServerMgr sound method: the field offsets
in the disassembly are all off by four.

```asm
10034044:  8b d9              mov    %ecx,%ebx        ; this = incoming ecx, NO adjust
10034054:  39 bb 6c 03 00 00  cmp    %edi,0x36c(%ebx) ; m_pWorldFile is at 0x370!
```

`CServerMgr : public CBaseMgr, public CServerSoundMgr` — PlaySound overrides
the *second* base, so it receives the sound subobject (`CServerMgr + 4`) as
`this`. VC6 does not emit an adjustor thunk; it compiles the whole body
against the subobject pointer and folds the `-4` into every field access.
You write normal member code (`m_pWorldFile`, `m_Bank28`, `m_Time`) and the
compiler produces the shifted offsets. The one place the adjustment surfaces
is a call to a *first-base* method:

```asm
10034170:  8d 4b fc           lea    -0x4(%ebx),%ecx  ; this-4 = the CServerMgr
10034173:  e8 17 03 00 00     call   0x1003448f       ; GetSoundTrack
```

`lea -4(%ebx),%ecx` before a thiscall is the signature of second-base code
calling back into the main object. If you see it, you're in a second-base
override; if you forget this, every offset in your reconstruction misses by 4
and nothing matches.

---

## 3 · Read the arguments off the stack, not out of Ghidra

Ghidra had the `GetSoundTrack` call as `GetSoundTrack(param_1)` — the info
struct. The frame says otherwise:

```asm
1003416d:  ff 75 08           push   0x8(%ebp)        ; the argument
```

`[ebp+8]` is the first parameter slot — but the parameter is *dead* in memory
by this point (it lives in `esi`), and the fs_GetFileIdentifier call earlier
did `lea 0x8(%ebp),%ecx; push %ecx` — it passed `&hFile` pointing **into the
dead parameter slot**. VC6 reuses dead parameter homes for locals. So
`[ebp+8]` is `hFile`, and the call is `GetSoundTrack(hFile)`, not
`GetSoundTrack(pPlaySoundInfo)`. Both compile; both produce 494 bytes; only
one pushes the right slot.

The same trick decodes the two locals:

* `-4(%ebp)` — zeroed at entry, written from `eax` after GetSoundTrack,
  pushed as SoundInstance::Init's last argument → `pTrack`.
* `-8(%ebp)` — written from the filename `lea` early, then written from
  `lea 0x24(%ebx)` (= `&m_Bank28`) late → **two** locals sharing one slot,
  `pSoundName` then `pBank`. VC6 colors stack slots by live range; disjoint
  locals share. Seeing two unrelated values in one slot means two source
  variables, not one.

---

## 4 · Let the SDK write the middle of the function

Three chunks of the body are macros/inlines you should never hand-roll:

* The radius clamps compile to the branchless-looking
  `flds/fcomps/ja/fstps` ladder of **LTCLAMP** (`ltbasedefs.h`).
* The fire-and-forget path copies 0x138 bytes with `push $0x138 … call memcpy`
  — that's **PLAYSOUNDINFO_COPY**, which the SDK defines as
  `memcpy(&dest, &src, sizeof(PlaySoundInfo))`.
* The nine stores after a successful Init are **dl_AddHead** — the LTList
  inline from `de_objects.h`, same as everywhere else in the engine.

The freelist return on Init failure is the one piece of engine-internal
inlining: `*(void**)pSound = pBank->m_pFreeList; pBank->m_pFreeList = pSound;`
— StructBank's intrusive free list, two stores, no call.

---

## 5 · The mnemonic diff: finding structure inside 494 bytes

With the byte diff (from [02](02-the-loop.md)) showing hundreds of scattered
mismatches, switch tools. Normalize both instruction streams — every address
and immediate to `X` — and diff *those*. `tools/fndiff.py` does exactly this
(and then tries every renaming of the callee-saved registers, to separate
allocation from structure):

```sh
tools/fndiff.py 0x10034037 src/engine/server/Release/smgr.obj PlaySound \
    --image=out/reference/server.dll
```

A wrong calling convention, a missing local, a misplaced branch — each shows
up as an *inserted or deleted instruction*, while a register-allocation
difference shows as paired `-mov X,%esi / +mov X,%edi` lines. This diff is
what separated the fixable from the cosmetic at every step below.

What it caught here, in order:

1. **`GetSoundTrack` and `SoundInstance::Init` are thiscall members** —
   ours passed them as cdecl helpers; the diff showed the extra pushes.
2. **`GetSoundTrack` takes `hFile`** (section 3) — `push 0x8(%ebp)` vs
   `push %esi`.
3. **The GETHANDLE store is if-first**: `if (flags & GETHANDLE)
   info->m_hSound = pSound; else info->m_hSound = LTNULL;` — else-first
   swaps the branch polarity.
4. And after all of those: 117 remaining diff lines that were *one*
   difference — every `%esi` and `%edi` swapped, plus a spill. Register
   allocation. Which is not cosmetic noise to shrug at; it's the compiler
   telling you your *variables* are wrong, as follows.

---

## 6 · Register allocation is a message about your source

The final state before the match: identical instruction count, identical
structure, but ship bound `pPlaySoundInfo→esi` with `edi` holding zero for
the whole function, while ours bound `info→edi` with a short-lived zero in
`eax`. You cannot fix that by renaming registers; you fix it by asking *why
the ship compiler thought a zero deserved a callee-saved register*.

Trace edi's life in the ship function:

```asm
10034042:  33 ff     xor    %edi,%edi          ; born zero
10034046:  3b f7     cmp    %edi,%esi          ; null-checks compare against it
10034048:  89 7d fc  mov    %edi,-0x4(%ebp)    ; initializes pTrack
...
1003418b:  8b f8     mov    %eax,%edi          ; becomes the sb_Allocate result
...
1003421a:  89 38     mov    %edi,(%eax)        ; hResult = it — on BOTH paths
```

`edi` is **`SoundInstance* pSound = LTNULL;`** — the "zero register" and the
result variable are the same variable. It's zero-born at the top, feeds every
early comparison, survives across three calls (hence callee-saved), and the
final `hResult = (HLTSOUND)pSound` store is shared by the allocate path
(where it holds the new instance) and the fire-and-forget path (where it
still holds LTNULL). One source variable explains the entire allocation.

Ours had folded that final store into `andl $0,(%eax)` on the fire-and-forget
path — because our source *returned separately* from that branch, so the
compiler could prove the constant. The fix was never a register hint; it was
function shape.

---

## 7 · Function shape: where the returns are decides everything

Three structurally equivalent versions of this function compile to three
different binaries. The rules that fell out:

**Rule 1 — a shared tail must be written once, reached by fallthrough.**
Ship's `hResult = (HLTSOUND)pSound; return LT_OK;` exists once in source,
after the `if/else`, reached by both branches. Writing `return LT_OK` inside
each branch produces either duplicated tails or a const-folded store.

**Rule 2 — failure paths return early; the success tail is the last
statement.** Every failure in the keep-the-sound path is an explicit
`return LT_ERROR;`. VC6 merges them all into one `push 1; pop eax` stub —
placed mid-function at the *last* failure site (the Init-failure
fallthrough), jumping forward to the epilogue.

**Rule 3 — the epilogue attaches to the lexically last return.** This was
the final two-block diff: with the function wrapped in
`if (m_pWorldFile && …) { … } return LT_ERROR;`, the trailing error return
is the last statement, so the merged error stub *keeps the epilogue* and the
LT_OK tail has to jump backward to it. Inverting the guards —

```cpp
if (!m_pWorldFile)
    return LT_ERROR;
if ((flags & PLAYSOUND_ATTACHED) && !pPlaySoundInfo->m_hObject)
    return LT_ERROR;
```

— makes `return LT_OK` lexically last, the epilogue lands at the bottom, the
error stub becomes `push 1; pop eax; jmp epilogue`, and the function matched.
Same logic, same 494 bytes, different layout: **guard polarity is
observable** in the block order.

(Corollary from the same build: `return LT_INVALIDPARAMS` compiled to
`push $0x3c; jmp <the pop eax of the error stub>` — VC6 cross-jumps *into
the middle* of another return sequence. Don't fight it; it falls out free
once the shape is right.)

---

## 8 · The finished source

```cpp
LTRESULT CServerMgr::PlaySound(PlaySoundInfo* pPlaySoundInfo, HLTSOUND& hResult)
{
    SoundInstance* pSound = LTNULL;
    void* pTrack = LTNULL;
    char* pSoundName;
    void* hFile;

    if (!pPlaySoundInfo)
        return LT_INVALIDPARAMS;

    if (!m_pWorldFile)
        return LT_ERROR;

    if ((pPlaySoundInfo->m_dwFlags & PLAYSOUND_ATTACHED) && !pPlaySoundInfo->m_hObject)
        return LT_ERROR;

    pSoundName = pPlaySoundInfo->m_szSoundName;
    if (!fs_GetFileIdentifier(&m_FileMgr, pSoundName, 0, &hFile))
    {
        if (g_DebugLevel >= 2)
            dsi_ServerError("Missing sound file %s", pSoundName);

        return LT_MISSINGFILE;
    }

    if (pPlaySoundInfo->m_dwFlags & (PLAYSOUND_AMBIENT | PLAYSOUND_3D))
    {
        pPlaySoundInfo->m_fOuterRadius =
            LTCLAMP(pPlaySoundInfo->m_fOuterRadius, 1.0f, 65000.0f);
        pPlaySoundInfo->m_fInnerRadius =
            LTCLAMP(pPlaySoundInfo->m_fInnerRadius, 1.0f, 65000.0f);
    }

    if ((pPlaySoundInfo->m_dwFlags & PLAYSOUND_CTRL_VOL) && pPlaySoundInfo->m_nVolume == 100)
        pPlaySoundInfo->m_dwFlags &= ~PLAYSOUND_CTRL_VOL;

    if ((pPlaySoundInfo->m_dwFlags & PLAYSOUND_CTRL_PITCH) && pPlaySoundInfo->m_fPitchShift == 1.0f)
        pPlaySoundInfo->m_dwFlags &= ~PLAYSOUND_CTRL_PITCH;

    if (pPlaySoundInfo->m_dwFlags & (PLAYSOUND_TIMESYNC | PLAYSOUND_TIME |
        PLAYSOUND_GETHANDLE | PLAYSOUND_ATTACHED | PLAYSOUND_LOOP))
    {
        if (pPlaySoundInfo->m_dwFlags & PLAYSOUND_ATTACHED)
            pPlaySoundInfo->m_dwFlags &= ~PLAYSOUND_CLIENTLOCAL;

        if ((pPlaySoundInfo->m_dwFlags & (PLAYSOUND_TIMESYNC | PLAYSOUND_TIME | PLAYSOUND_ATTACHED)) &&
            !(pPlaySoundInfo->m_dwFlags & PLAYSOUND_LOOP) &&
            (pTrack = GetSoundTrack(hFile)) == LTNULL)
        {
            return LT_ERROR;
        }

        StructBank* pBank = &m_Bank28;
        pSound = (SoundInstance*)sm_sb_Allocate(pBank);
        if (!pSound)
            return LT_ERROR;

        if (pPlaySoundInfo->m_dwFlags & PLAYSOUND_GETHANDLE)
            pPlaySoundInfo->m_hSound = (HLTSOUND)pSound;
        else
            pPlaySoundInfo->m_hSound = LTNULL;

        if (!pSound->Init(pPlaySoundInfo, m_Time, hFile, pTrack))
        {
            pPlaySoundInfo->m_hSound = LTNULL;
            *(void**)pSound = pBank->m_pFreeList;
            pBank->m_pFreeList = pSound;
            return LT_ERROR;
        }

        dl_AddHead(&m_List54, &pSound->m_Link, pSound);
    }
    else
    {
        SoundRecord* pRecord = (SoundRecord*)sm_AddSoundRecord(this, 0);
        PLAYSOUNDINFO_COPY(pRecord->m_Info, *pPlaySoundInfo);
        pRecord->m_hFile = hFile;
    }

    // pSound is still LTNULL on the fire-and-forget path.
    hResult = (HLTSOUND)pSound;
    return LT_OK;
}
```

(The copy in `smgr.cpp` carries a few explanatory comments as well.)
`build/engine.sh server-one smgr.cpp PlaySound` reports
`MATCH 0x10034037 … (494)`. `smgr.cpp` is also compiled into lithtech.exe —
`build/engine.sh lithtech-one smgr.cpp PlaySound` measures the client's copy,
which `/O2` lays out differently.

## The checklist this leaves behind

When a big function is instruction-equivalent but won't converge:

1. Wrong offsets everywhere by a constant → you're in a second-base override.
2. `lea -4(this)` before a thiscall → a call back into the first base.
3. Arguments pushed from `[ebp+8..]` after the parameter is in a register →
   a local reusing a dead parameter slot; read the *slot's* current owner.
4. One stack slot written from two unrelated values → two live-range-disjoint
   locals, not one.
5. A zero in a callee-saved register that survives calls → a pointer local
   initialized to LTNULL and *used at the end*, not "a zero register".
6. A constant store where ship has a register store → your source lets the
   compiler prove the constant; ship's shares a variable across paths.
7. Epilogue in the "wrong" place → look at which return is lexically last;
   invert guards until the success tail is the final statement.
