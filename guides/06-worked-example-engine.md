# 06 · A worked example (engine binary)

[04-worked-example](04-worked-example.md) matched a library function where we
had symbols and era source. This one is the hard case and the one you'll
actually repeat: a function inside **server.dll** — no symbols, no
relocations, no source.

We'll reconstruct `CLTServer::WriteToMessageByte`, then show how one solved
function unlocks a whole cluster. Every byte here is real; the result is in
`src/engine/server/servermsg.cpp`.

---

## 1 · Find a target and learn its name

server.dll has no symbols, but it *does* carry `Class::Method` strings —
they're arguments to an internal error-reporting macro. `tools/tagmap.py`
finds every such literal, every push of its address, and the function doing
the pushing, which names it:

```sh
tools/tagmap.py out/reference/server.dll out/reference/server-functions.csv
```

The `ILTCSBase` vtable at `0x1004e328` gave the message-writer cluster: a
dozen ~71-byte functions. Start with one:

```sh
tools/exedump.py out/reference/server.dll 0x10006040 0x10006087
```

```asm
10006040:  8b 4c 24 04     mov    0x4(%esp),%ecx        ; hMessage
10006044:  85 c9           test   %ecx,%ecx
10006046:  75 33           jne    0x1000607b
10006048:  6a 3c           push   $0x3c                 ; 0x3c = 60 = LT_INVALIDPARAMS
1000604a:  e8 d2 0c 03 00  call   0x10036d21            ; empty hook (a bare `ret`)
1000604f:  83 3d f0 6f .. 02  cmpl $0x2,0x10056ff0      ; if (g_DebugLevel >= 2)
10006056:  59              pop    %ecx                  ; clean the pushed 0x3c
10006057:  7c 1d           jl     0x10006076
10006059:  68 14 11 05 10  push   $0x10051114
1000605e:  68 10 11 05 10  push   $0x10051110
10006063:  68 78 12 05 10  push   $0x10051278  ; "ILTCSBase::WriteToMessageByte"
10006068:  ff 35 c0 10 05 10  push 0x100510c0  ; "LT ERROR: %s returned %s (%s)"
1000606e:  e8 86 83 00 00  call   0x1000e3f9            ; the error printer
10006073:  83 c4 10        add    $0x10,%esp
10006076:  6a 3c           push   $0x3c
10006078:  58              pop    %eax                  ; return 60
10006079:  eb 09           jmp    0x10006084
1000607b:  8b 01           mov    (%ecx),%eax           ; hMessage's vtable
1000607d:  ff 74 24 08     push   0x8(%esp)             ; val
10006081:  ff 50 40        call   *0x40(%eax)           ; hMessage->vtbl[16](val)
10006084:  c2 08 00        ret    $0x8
```

That reads cleanly: *validate the handle, report an error and return
LT_INVALIDPARAMS if it's null, otherwise forward through the message object's
own vtable at offset 0x40.*

## 2 · Model what isn't in the public SDK

Two things here have no SDK declaration and must be reconstructed:

**The error macro.** It's LithTech's internal `RETURN_ERROR` (see
`src/engine/server/de_error.h`). The shipped code pushes *different* strings
at different call sites (a literal `"LT_INVALIDPARAMS"` in one cluster, a
`.data` global in another) — but both encode as `push imm32`, and
**relocation targets are masked by the diff tools**, so a single macro
reproduces every variant.

**The message object.** `HMESSAGEWRITE` is an engine-private class. All we
know — and all we need — is that a write goes through vtable offset `0x40`.
So declare a class with enough padding slots to put `WriteByte` exactly
there (`src/engine/server/servermsg.h`):

```cpp
class IMessageWrite
{
public:
    virtual void     EndWrite()=0;              // 0x00
    virtual LTRESULT _p01()=0; /* … */
    virtual LTRESULT WriteByte(uint8 val)=0;    // 0x40  ← slot 16
    ...
};
```

Placeholder slots are fine; only the *offsets* matter for byte identity. (As
more of the class is understood, placeholders get real names — in the
current header, slot 2 is `ReadWord`.)

## 3 · Write it

```cpp
LTRESULT CLTServer::WriteToMessageByte(HMESSAGEWRITE_I hMessage, uint8 val)
{
    if (!hMessage)
        RETURN_ERROR_PARAM("ILTCSBase::WriteToMessageByte");

    return hMessage->WriteByte(val);
}
```

## 4 · Compile — and discover the optimization level

Compile the file by hand at `/O2` first, the way the game DLLs are built
(inside `make shell`; `build/env.sh` defines `run_cl`):

```sh
source build/env.sh
cd src/engine/server
run_cl /nologo /MT /W3 /O2 /D WIN32 /D NDEBUG /D _WINDOWS /D NO_PRAGMA_LIBS \
    /I ..\\..\\LT2\\sdk\\inc /I . /c /FoRelease/ servermsg.cpp
```

80 bytes, but the shipped function is 71. The differences are *all* one kind:

| ours (`/O2`) | shipped | what it is |
|---|---|---|
| `mov eax,[g_DebugLevel]` + `cmp $2,%eax` | `cmpl $2,[g_DebugLevel]` | compare against memory directly |
| `add $0x4,%esp` | `pop %ecx` | 1-byte stack cleanup |
| `mov $0x3c,%eax` | `push $0x3c; pop %eax` | 3 bytes instead of 5 |

Every one trades speed for size — the signature of **`/O1` (minimize size)**.
Rebuild with `/O1` and it matches exactly. That is what `build/engine.sh
server` uses now.

> **server.dll is built `/O1`; the game DLLs and lithshared libs are `/O2`.**
> Getting this wrong makes every function in the binary "nearly right" — a
> whole-binary error that looks like dozens of small ones.

## 5 · Verify

```sh
build/engine.sh server-one servermsg.cpp WriteToMessageByte
```

```
  MATCH 0x10006040   ?WriteToMessageByte@CLTServer@@QAEKPAVIMessageWrite@@E@Z (71)
```

Our object's bytes and the shipped bytes are identical everywhere except the
relocation slots (zeros in our `.obj`, filled addresses in the shipped image)
— which is exactly what `exematch` masks.

## 6 · Cash in the cluster

The other eleven writers differ only in the tag string, the argument type and
the vtable offset — all readable from the binary the same way. They were
written and matched in one pass. Same for the fourteen readers and the
fifteen rotation/matrix helpers: **41 functions from three shapes.**

This is the rhythm of engine work: the first function in a family is
expensive (you're modelling the macro, the object layout, the flags), and the
rest are nearly free. Pick your first target so it *is* the start of a
family.

---

## What the bytes will tell you that the headers won't

Three corrections the disassembly forced in this cluster — the SDK header
would have led you astray on every one:

**1 · The header is a different era.** `src/LT2/sdk/inc/` is the NOLF2-era
SDK; these binaries are NOLF1. Method *signatures* are mostly stable, but
**vtable order is not** — the header's slot 2 is a 1-argument function where
the shipped slot 2 takes 2 (`ret 8`). Derive layout from the binary; use the
header for types.

**2 · Arguments get reordered on the way through.**
`GetRotationVectors(pRotation, pUp, pRight, pForward)` forwards its inner
call as `(pRotation, pRight, pUp, pForward)` — Up and Right swapped. Only the
push order in the disassembly reveals this.

**3 · Not every pointer is validated.** `AlignRotation` guards `pRotation`
and `pVector` but *not* its optional `pUp`. Guarding it too adds one compare
and the function no longer matches. The guard set is data, not a style
choice.

## The embedded-interface pattern

You will meet this constantly in the engine. When you see:

```asm
mov  0x4(%ecx),%eax     ; vptr loaded from this+4
add  $0x4,%ecx          ; this  = this+4
call *0x28(%eax)        ; slot 10 on that sub-object
```

…that's a **thiscall on an object embedded at offset +4** — not a member
function pointer, and not a member *pointer* (a pointer member would load the
pointer first, then its vptr). To reproduce it, give the outer class its own
vptr at +0, embed the implementation object at +4, and dispatch through a
pointer to it, so VC6 makes a real virtual call instead of devirtualizing.
`src/engine/server/servermath.h` / `servermath.cpp` is the live example —
the embedded object is the SDK's own `ILTMath`:

```cpp
class CLTServerMath
{
public:
    virtual void _ownvirtual();   // puts this class's vptr at +0
    ILTMath m_common;             // lands at +4
    ...
};

void CLTServerMath::EulerRotateX(LTRotation* pRotation, float amount)
{
    if (!pRotation)
        RETURN_ERROR_VOID("ILTCSBase::EulerRotateX");

    ILTMath* pC = &m_common;      // through a pointer: a real virtual call
    pC->EulerRotateX(*pRotation, amount);
}
```

Calling `m_common.EulerRotateX(...)` on the member directly lets VC6 call the
known implementation, and the bytes would not match.

## Two more `/O1` idioms worth memorising

**`push` as a stack reservation.** In a float forward you'll see:

```asm
push %ecx        ; ← NOT an argument: `push reg` is a 1-byte `sub esp,4`
fstps (%esp)     ; store the float into the space just reserved
```

Don't try to model that `push %ecx` as a parameter — it's the compiler
grabbing 4 bytes for the float argument.

**`and dword ptr [x],0` stores zero** (shorter than `mov [x],0`). But *where*
you write the initialisation matters:

```cpp
void* val;
if (!hMessage) RETURN_ERROR_VALUE(..., NULL);
val = NULL;                      // ← init AFTER the guard: reuses the arg slot
hMessage->ReadMessage(&val);
return val;
```

Writing `void* val = NULL;` at the declaration makes VC6 hoist the store
above the guard **and allocate a separate stack slot** instead of reusing the
(now dead) incoming argument slot — a 4-byte frame difference that breaks the
match. That argument-slot reuse is also why these readers carry an `ebp`
frame at all.

---

## Where to go next

For small unclaimed targets, list the `todo` rows largest-last:

```sh
python3 tools/progress.py --todo=server.dll | sort -k3 -n | head
```

`tools/tagmap.py ... --undone` lists the functions an error-tag literal names
that are not reconstructed yet. Read
[03-reading-disassembly](03-reading-disassembly.md) for the general idiom
dictionary, [07-vtables](07-vtables.md) to name functions that have no tag,
and [08-worked-example-playsound](08-worked-example-playsound.md) for a large
function taken all the way to byte identity.
