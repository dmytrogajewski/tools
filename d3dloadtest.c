/* Load a linked d3d.ren the way lithtech.exe does, with its preferred base
 * deliberately occupied so the loader must relocate it through the rebuilt
 * .reloc section, then exercise the three exports:
 *   GetSupportedModes / FreeModeList -- enumerate the DirectDraw devices'
 *     display modes into an RMode list and free it (under a headless Wine
 *     the list may be empty; the calls must still return);
 *   RenderDLLSetup -- fill a zeroed RenderStruct. The renderer's 37 function
 *     pointers live at +0x6c..+0xfc (src/engine/d3d/d3d.cpp maps the struct);
 *     every one must be set and point into the loaded image.
 * Nothing here needs a window: Init (slot +0x6c) is not called. */
#include <windows.h>
#include <stdio.h>

typedef void* (*GetSupportedModes_t)(void);
typedef void  (*FreeModeList_t)(void*);
typedef void  (*RenderDLLSetup_t)(void*);

int main(int argc, char** argv)
{
    HMODULE h;
    GetSupportedModes_t getModes;
    FreeModeList_t freeModes;
    RenderDLLSetup_t setup;
    static unsigned char rs[0x400];     /* RenderStruct is 0x148; room to spare */
    IMAGE_NT_HEADERS* nt;
    DWORD lo, hi, i, set = 0, inside = 0;
    void* modes;
    int n;

    setvbuf(stdout, NULL, _IONBF, 0);   /* a crash must not eat the log */

    if (argc > 2)
        printf("base 0x10000000 %s\n",
               VirtualAlloc((void*)0x10000000, 0x1000, MEM_RESERVE, PAGE_NOACCESS)
                   ? "occupied" : "NOT occupied");

    h = LoadLibraryA(argv[1]);
    if (!h) { printf("LoadLibrary FAILED: %lu\n", GetLastError()); return 1; }
    printf("loaded at %p%s\n", (void*)h,
           ((DWORD)h != 0x10000000) ? " (relocated)" : "");
    nt = (IMAGE_NT_HEADERS*)((char*)h + ((IMAGE_DOS_HEADER*)h)->e_lfanew);
    lo = (DWORD)h;
    hi = lo + nt->OptionalHeader.SizeOfImage;

    getModes  = (GetSupportedModes_t)GetProcAddress(h, "GetSupportedModes");
    freeModes = (FreeModeList_t)GetProcAddress(h, "FreeModeList");
    setup     = (RenderDLLSetup_t)GetProcAddress(h, "RenderDLLSetup");
    if (!getModes || !freeModes || !setup) { printf("exports missing\n"); return 1; }

    modes = getModes();
    printf("GetSupportedModes -> %p\n", modes);
    if (modes)
    {
        /* RMode (LT2/sdk/inc/ltbasedefs.h): m_bHardware, m_RenderDLL[256],
         * m_InternalName[128] at +0x104, m_Description[128] at +0x184,
         * m_Width/m_Height/m_BitDepth at +0x204, m_pNext at +0x210 */
        char* p = (char*)modes;
        printf("  first: \"%.128s\" %ux%ux%u\n", p + 0x184,
               *(unsigned*)(p + 0x204), *(unsigned*)(p + 0x208), *(unsigned*)(p + 0x20c));
        for (n = 0; p && n < 10000; n++)
            p = *(char**)(p + 0x210);
        printf("  %d mode(s)\n", n);
        freeModes(modes);
        printf("FreeModeList OK\n");
    }

    setup(rs);
    for (i = 0x6c; i <= 0xfc; i += 4)
    {
        DWORD v = *(DWORD*)(rs + i);
        if (v) set++;
        if (v >= lo && v < hi) inside++;
    }
    printf("RenderDLLSetup: %lu of 37 slots set, %lu inside the image\n", set, inside);
    return (set == 37 && inside == 37) ? 0 : 1;
}
