#include "native.h"
#include "profiles/fl_26_1_7_5654.h"
#include <wincrypt.h>
#include <string.h>

typedef unsigned char (*TrackValid)(int track);
typedef unsigned char (*SlotOccupied)(void *slot);
typedef void *(*MakeString)(void **out, const char *utf8);
typedef void *(*FreeString)(void **data);
typedef void (*PrepareInsert)(void *manager, void *label, int track, int slot);
typedef void (*LoadPreset)(void *slot, int mode, void *path, void *display_name,
                           unsigned char apply, unsigned char show_editor);

static int matches_engine_file(const wchar_t *path) {
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE,
                              NULL, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, NULL);
    if (file == INVALID_HANDLE_VALUE) {
        return 0;
    }

    HCRYPTPROV provider = 0;
    HCRYPTHASH hash = 0;
    BYTE buffer[64 * 1024], digest[sizeof(engine_sha256)];
    DWORD digest_size = sizeof(digest), read = 0;
    BOOL ok = CryptAcquireContextW(&provider, NULL, NULL, PROV_RSA_AES, CRYPT_VERIFYCONTEXT)
        && CryptCreateHash(provider, CALG_SHA_256, 0, 0, &hash);
    while (ok) {
        ok = ReadFile(file, buffer, sizeof(buffer), &read, NULL);
        if (!ok || !read) {
            break;
        }
        ok = CryptHashData(hash, buffer, read, 0);
    }
    ok = ok && CryptGetHashParam(hash, HP_HASHVAL, digest, &digest_size, 0)
        && digest_size == sizeof(digest) && !memcmp(digest, engine_sha256, sizeof(digest));
    if (hash) {
        CryptDestroyHash(hash);
    }
    if (provider) {
        CryptReleaseContext(provider, 0);
    }
    CloseHandle(file);
    return ok;
}

static int read_memory(const void *address, void *value, size_t size) {
    MEMORY_BASIC_INFORMATION info;
    if (!VirtualQuery(address, &info, sizeof(info)) || info.State != MEM_COMMIT ||
        (info.Protect & (PAGE_NOACCESS | PAGE_GUARD))) {
        return 0;
    }
    uintptr_t delta = (uintptr_t)address - (uintptr_t)info.BaseAddress;
    if (delta > info.RegionSize || size > info.RegionSize - delta) {
        return 0;
    }
    memcpy(value, address, size);
    return 1;
}

static int read_pointer(const void *address, void **value) {
    return read_memory(address, value, sizeof(*value)) && *value != NULL;
}

PyObject *initialize_engine(PyObject *self, PyObject *unused) {
    (void)unused;
    ModuleState *state = PyModule_GetState(self);
    HMODULE engine = GetModuleHandleW(L"FLEngine_x64.dll");
    wchar_t path[4096];
    DWORD length = engine ? GetModuleFileNameW(engine, path, 4096) : 0;
    if (!length || length >= 4096 ||
        (state->engine != (unsigned char *)engine && !matches_engine_file(path))) {
        PyErr_SetString(PyExc_RuntimeError, "Expected the verified FL Studio 26.1.7.5654 x64 engine");
        return NULL;
    }
    state->engine = (unsigned char *)engine;
    return PyUnicode_FromWideChar(path, length);
}

PyObject *mixer_insert_empty_slot(PyObject *self, PyObject *args) {
    const char *preset;
    int track, slot_index;
    if (!PyArg_ParseTuple(args, "sii:insert_empty_slot", &preset, &track, &slot_index)) {
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    void *thread_address;
    DWORD main_thread;
    if (!engine || engine != (unsigned char *)GetModuleHandleW(L"FLEngine_x64.dll") ||
        !read_pointer(engine + MAIN_THREAD, &thread_address) ||
        !read_memory(thread_address, &main_thread, sizeof(main_thread)) ||
        main_thread != GetCurrentThreadId()) {
        PyErr_SetString(PyExc_RuntimeError, "Run the verified effect loader on the FL idle thread");
        return NULL;
    }

    TrackValid valid_track = (TrackValid)(engine + TRACK_VALID);
    if (!*preset || track < 0 || track > 500 || !valid_track(track) ||
        slot_index < 0 || slot_index >= 10) {
        PyErr_SetString(PyExc_ValueError, "Invalid FL Mixer track, slot or preset path");
        return NULL;
    }

    void *mixer, *slot, *vtable, *entry;
    if (!read_pointer(engine + MIXER_ARRAY, &mixer) ||
        !read_pointer((const unsigned char *)mixer + track * TRACK_STRIDE +
                      SLOT_ARRAY + slot_index * sizeof(void *), &slot) ||
        !read_pointer(slot, &vtable) ||
        !read_pointer((const unsigned char *)vtable + LOAD_EFFECT, &entry)) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide a valid Mixer effect slot");
        return NULL;
    }
    SlotOccupied occupied = (SlotOccupied)(engine + SLOT_OCCUPIED);
    if (occupied(slot)) {
        PyErr_SetString(PyExc_ValueError, "The requested slot is already occupied");
        return NULL;
    }
    uintptr_t entry_rva = (uintptr_t)entry - (uintptr_t)engine;
    if (entry_rva < TEXT_START || entry_rva >= TEXT_END) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide a compatible effect loader");
        return NULL;
    }

    void *global_manager, *manager;
    if (!read_pointer(engine + PROJECT_MANAGER, &global_manager) ||
        !read_pointer(global_manager, &manager)) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide its project manager");
        return NULL;
    }

    void *path = NULL, *label = NULL;
    MakeString make_string = (MakeString)(engine + FROM_UTF8);
    FreeString free_string = (FreeString)(engine + FREE_STRING);
    make_string(&path, preset);
    if (!path) {
        PyErr_SetString(PyExc_ValueError, "FL could not construct the effect preset path");
        return NULL;
    }
    make_string(&label, "Insert Mixer effect");
    if (!label) {
        free_string(&path);
        PyErr_SetString(PyExc_RuntimeError, "FL could not name the effect insertion");
        return NULL;
    }

    /* Record FL's normal per-slot undo state before applying the preset. */
    PrepareInsert prepare = (PrepareInsert)(engine + PREPARE_INSERT);
    prepare(manager, label, track, slot_index);
    free_string(&label);

    LoadPreset load = (LoadPreset)entry;
    load(slot, -3, path, NULL, 1, 0);
    free_string(&path);
    return PyLong_FromUnsignedLong(0);
}

/* ---- Effect slot remove/replace ----
   FL's own "(delete) effect" FST (shipped in <engine>/Data/System) clears a
   slot when loaded into it; loading any FST into an occupied slot replaces
   the effect. Both go through the same verified preset loader used for
   inserts, with FL's per-slot undo recorded first. */

static int engine_data_dir(unsigned char *engine, wchar_t *dir, size_t chars) {
    wchar_t path[4096];
    DWORD len = GetModuleFileNameW((HMODULE)engine, path, 4096);
    if (!len || len >= 4096) {
        return 0;
    }
    wchar_t *slash = wcsrchr(path, L'\\');
    if (!slash) {
        return 0;
    }
    *slash = 0;
    return swprintf(dir, chars, L"%s\\Data\\System", path) > 0;
}

PyObject *mixer_remove_effect(PyObject *self, PyObject *args) {
    int track, slot_index;
    if (!PyArg_ParseTuple(args, "ii:remove_effect", &track, &slot_index)) {
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    void *thread_address;
    DWORD main_thread;
    if (!engine || engine != (unsigned char *)GetModuleHandleW(L"FLEngine_x64.dll") ||
        !read_pointer(engine + MAIN_THREAD, &thread_address) ||
        !read_memory(thread_address, &main_thread, sizeof(main_thread)) ||
        main_thread != GetCurrentThreadId()) {
        PyErr_SetString(PyExc_RuntimeError, "Run the verified effect loader on the FL idle thread");
        return NULL;
    }

    TrackValid valid_track = (TrackValid)(engine + TRACK_VALID);
    if (track < 0 || track > 500 || !valid_track(track) ||
        slot_index < 0 || slot_index >= 10) {
        PyErr_SetString(PyExc_ValueError, "Invalid FL Mixer track or slot");
        return NULL;
    }

    void *mixer, *slot, *vtable, *entry;
    if (!read_pointer(engine + MIXER_ARRAY, &mixer) ||
        !read_pointer((const unsigned char *)mixer + track * TRACK_STRIDE +
                      SLOT_ARRAY + slot_index * sizeof(void *), &slot) ||
        !read_pointer(slot, &vtable) ||
        !read_pointer((const unsigned char *)vtable + LOAD_EFFECT, &entry)) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide a valid Mixer effect slot");
        return NULL;
    }
    SlotOccupied occupied = (SlotOccupied)(engine + SLOT_OCCUPIED);
    if (!occupied(slot)) {
        PyErr_SetString(PyExc_ValueError, "The requested slot is already empty");
        return NULL;
    }
    uintptr_t entry_rva = (uintptr_t)entry - (uintptr_t)engine;
    if (entry_rva < TEXT_START || entry_rva >= TEXT_END) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide a compatible effect loader");
        return NULL;
    }

    void *global_manager, *manager;
    if (!read_pointer(engine + PROJECT_MANAGER, &global_manager) ||
        !read_pointer(global_manager, &manager)) {
        PyErr_SetString(PyExc_RuntimeError, "FL did not provide its project manager");
        return NULL;
    }

    wchar_t dir[2048];
    if (!engine_data_dir(engine, dir, 2048)) {
        PyErr_SetString(PyExc_RuntimeError, "Cannot locate the FL Data/System directory");
        return NULL;
    }
    char path_utf8[4200];
    int plen = WideCharToMultiByte(CP_UTF8, 0, dir, -1, path_utf8,
                                   sizeof(path_utf8), NULL, NULL);
    if (!plen) {
        PyErr_SetString(PyExc_RuntimeError, "Path conversion failed");
        return NULL;
    }
    path_utf8[plen - 1] = 0; /* drop the terminating NUL inside the buffer */
    strncat(path_utf8, "\\(delete) effect.fst", sizeof(path_utf8) - strlen(path_utf8) - 1);

    void *path = NULL, *label = NULL;
    MakeString make_string = (MakeString)(engine + FROM_UTF8);
    FreeString free_string = (FreeString)(engine + FREE_STRING);
    make_string(&path, path_utf8);
    if (!path) {
        PyErr_SetString(PyExc_ValueError, "FL could not construct the delete preset path");
        return NULL;
    }
    make_string(&label, "Remove Mixer effect");
    if (!label) {
        free_string(&path);
        PyErr_SetString(PyExc_RuntimeError, "FL could not name the effect removal");
        return NULL;
    }

    PrepareInsert prepare = (PrepareInsert)(engine + PREPARE_INSERT);
    prepare(manager, label, track, slot_index);
    free_string(&label);

    LoadPreset load = (LoadPreset)entry;
    load(slot, -3, path, NULL, 1, 0);
    free_string(&path);

    if (occupied(slot)) {
        PyErr_SetString(PyExc_RuntimeError,
                        "The effect was not removed (slot still occupied)");
        return NULL;
    }
    return PyLong_FromUnsignedLong(0);
}
