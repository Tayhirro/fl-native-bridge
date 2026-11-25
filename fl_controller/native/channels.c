#include "native.h"
#include "profiles/fl_26_1_7_5654.h"
#include <string.h>

typedef void *(*MakeString)(void **out, const char *utf8);
typedef void *(*FreeString)(void **value);
typedef void (*PrepareChannel)(void *manager, void *label, int index, int unused, unsigned char flag);
typedef void *(*InsertChannel)(int index, unsigned char unused, unsigned char flag);
typedef void (*LoadChannel)(void *channel, void *path, int mode, int flags,
                            void *unused, double position);

/* Channel objects and Mixer FX slots share a plugin-holder band; +0x58 is the
   holder's Delphi UnicodeString display name (the same field the reference
   build reads in GetChannelPluginAsync, and the same band the verified mixer
   slot name readback uses). Length is stored as int32 at ptr-4, UTF-16LE data
   at ptr. A failed/implausible read yields an empty name, never a wrong one. */
#define CHANNEL_PLUGIN_NAME_OFFSET 0x58
#define MAX_PLUGIN_NAME_CHARS 256

static int read_checked(const void *address, void *out, size_t size) {
    MEMORY_BASIC_INFORMATION info;
    if (!address || !VirtualQuery(address, &info, sizeof(info)) || info.State != MEM_COMMIT ||
        (info.Protect & (PAGE_NOACCESS | PAGE_GUARD))) return 0;
    uintptr_t offset = (uintptr_t)address - (uintptr_t)info.BaseAddress;
    if (offset > info.RegionSize || size > info.RegionSize - offset) return 0;
    memcpy(out, address, size);
    return 1;
}

/* This creates a Sampler, not a Playlist Audio Clip. The caller must report
   that the loaded source path cannot yet be read back independently. */
PyObject *channel_add_sampler(PyObject *self, PyObject *args) {
    const char *path;
    int expected_count;
    if (!PyArg_ParseTuple(args, "si:add_sampler", &path, &expected_count)) return NULL;
    if (expected_count < 0 || expected_count >= 4096 || !*path || strlen(path) > 8192) {
        PyErr_SetString(PyExc_ValueError, "Invalid sample path or expected channel count");
        return NULL;
    }
    wchar_t filename[8193];
    int chars = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, filename, 8193);
    DWORD attrs = chars ? GetFileAttributesW(filename) : INVALID_FILE_ATTRIBUTES;
    if (attrs == INVALID_FILE_ATTRIBUTES || (attrs & FILE_ATTRIBUTE_DIRECTORY)) {
        PyErr_SetString(PyExc_ValueError, "Sample path does not name a readable file");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    void *thread_address = NULL;
    DWORD thread;
    if (!engine || engine != (unsigned char *)GetModuleHandleW(L"FLEngine_x64.dll") ||
        !read_checked(engine + MAIN_THREAD, &thread_address, sizeof(thread_address)) ||
        !read_checked(thread_address, &thread, sizeof(thread)) || thread != GetCurrentThreadId()) {
        PyErr_SetString(PyExc_RuntimeError, "Run the verified channel operation on the FL idle thread");
        return NULL;
    }
    unsigned char *slot = NULL, *list = NULL;
    int count;
    void *global_manager = NULL, *manager = NULL;
    if (!read_checked(engine + CHANNEL_LIST, &slot, sizeof(slot)) || !slot ||
        !read_checked(slot, &list, sizeof(list)) || !list ||
        !read_checked(list + 0x10, &count, sizeof(count)) || count != expected_count ||
        !read_checked(engine + PROJECT_MANAGER, &global_manager, sizeof(global_manager)) ||
        !read_checked(global_manager, &manager, sizeof(manager)) || !manager) {
        PyErr_SetString(PyExc_ValueError, "Channel list or project manager changed; read again");
        return NULL;
    }
    MakeString make = (MakeString)(engine + FROM_UTF8);
    FreeString release = (FreeString)(engine + FREE_STRING);
    void *sample = NULL, *label = NULL;
    make(&sample, path);
    make(&label, "Insert sampler");
    if (!sample || !label) {
        if (sample) release(&sample);
        if (label) release(&label);
        PyErr_SetString(PyExc_RuntimeError, "FL could not construct the sample path or undo label");
        return NULL;
    }
    ((PrepareChannel)(engine + PREPARE_CHANNEL_INSERT))(manager, label, count, 0, 0);
    release(&label);
    void *channel = ((InsertChannel)(engine + INSERT_CHANNEL))(count, 0, 1);
    if (!channel) {
        release(&sample);
        PyErr_SetString(PyExc_RuntimeError, "Channel insertion was submitted but FL returned no channel");
        return NULL;
    }
    unsigned char **items = NULL, *observed = NULL;
    int after, kind;
    void **vtable = NULL, *entry = NULL;
    if (!read_checked(list + 0x10, &after, sizeof(after)) || after != count + 1 ||
        !read_checked(list + 8, &items, sizeof(items)) || !items ||
        !read_checked(items + count, &observed, sizeof(observed)) || observed != channel ||
        !read_checked((unsigned char *)channel + 0x160, &kind, sizeof(kind)) || kind != 0 ||
        !read_checked(channel, &vtable, sizeof(vtable)) || !vtable ||
        !read_checked(vtable + 0x150 / sizeof(void *), &entry, sizeof(entry)) || !entry ||
        (uintptr_t)entry < (uintptr_t)engine + TEXT_START ||
        (uintptr_t)entry >= (uintptr_t)engine + TEXT_END) {
        release(&sample);
        PyErr_SetString(PyExc_RuntimeError, "A channel was inserted but its identity or loader is unconfirmed");
        return NULL;
    }
    ((LoadChannel)entry)(channel, sample, 1, 0x42, NULL, -1.0);
    release(&sample);
    return PyLong_FromLong(count);
}

/* Reads the loaded generator's plugin display name from the channel's
   plugin-holder field. Returns an empty string when the field is absent or
   implausible, so a layout mismatch can only omit the name, never invent one. */
static int read_plugin_name(const unsigned char *channel, wchar_t *buffer, size_t *chars) {
    void *name_ptr = NULL;
    *chars = 0;
    if (!read_checked(channel + CHANNEL_PLUGIN_NAME_OFFSET, &name_ptr, sizeof(name_ptr)) ||
        !name_ptr) {
        return 1;
    }
    int32_t length = 0;
    if (!read_checked((const unsigned char *)name_ptr - 4, &length, sizeof(length)) ||
        length < 0 || length > MAX_PLUGIN_NAME_CHARS) {
        return 0;
    }
    if (length > 0 &&
        !read_checked(name_ptr, buffer, (size_t)length * sizeof(wchar_t))) {
        return 0;
    }
    *chars = (size_t)length;
    return 1;
}

/* Appends a Generator (plugin-hosting) channel and loads an FST preset into
   it. This mirrors channel_add_sampler exactly except that LoadChannel's mode
   is 0 (load plugin/preset) rather than 1 (load sample). The plugin name is
   read back from the channel object so the loaded generator can be confirmed
   independently; an unreadable name is reported empty, not inferred. Unlike
   the Sampler path this yields a full verification, because the plugin name is
   observable while a Sampler's WAV path is not. */
PyObject *channel_add_generator(PyObject *self, PyObject *args) {
    const char *path;
    int expected_count;
    if (!PyArg_ParseTuple(args, "si:add_generator", &path, &expected_count)) return NULL;
    if (expected_count < 0 || expected_count >= 4096 || !*path || strlen(path) > 8192) {
        PyErr_SetString(PyExc_ValueError, "Invalid generator preset path or expected channel count");
        return NULL;
    }
    wchar_t filename[8193];
    int chars = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, filename, 8193);
    DWORD attrs = chars ? GetFileAttributesW(filename) : INVALID_FILE_ATTRIBUTES;
    if (attrs == INVALID_FILE_ATTRIBUTES || (attrs & FILE_ATTRIBUTE_DIRECTORY)) {
        PyErr_SetString(PyExc_ValueError, "Generator preset path does not name a readable file");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    void *thread_address = NULL;
    DWORD thread;
    if (!engine || engine != (unsigned char *)GetModuleHandleW(L"FLEngine_x64.dll") ||
        !read_checked(engine + MAIN_THREAD, &thread_address, sizeof(thread_address)) ||
        !read_checked(thread_address, &thread, sizeof(thread)) || thread != GetCurrentThreadId()) {
        PyErr_SetString(PyExc_RuntimeError, "Run the verified channel operation on the FL idle thread");
        return NULL;
    }
    unsigned char *slot = NULL, *list = NULL;
    int count;
    void *global_manager = NULL, *manager = NULL;
    if (!read_checked(engine + CHANNEL_LIST, &slot, sizeof(slot)) || !slot ||
        !read_checked(slot, &list, sizeof(list)) || !list ||
        !read_checked(list + 0x10, &count, sizeof(count)) || count != expected_count ||
        !read_checked(engine + PROJECT_MANAGER, &global_manager, sizeof(global_manager)) ||
        !read_checked(global_manager, &manager, sizeof(manager)) || !manager) {
        PyErr_SetString(PyExc_ValueError, "Channel list or project manager changed; read again");
        return NULL;
    }
    MakeString make = (MakeString)(engine + FROM_UTF8);
    FreeString release = (FreeString)(engine + FREE_STRING);
    void *preset = NULL, *label = NULL;
    make(&preset, path);
    make(&label, "Insert generator");
    if (!preset || !label) {
        if (preset) release(&preset);
        if (label) release(&label);
        PyErr_SetString(PyExc_RuntimeError, "FL could not construct the preset path or undo label");
        return NULL;
    }
    ((PrepareChannel)(engine + PREPARE_CHANNEL_INSERT))(manager, label, count, 0, 0);
    release(&label);
    void *channel = ((InsertChannel)(engine + INSERT_CHANNEL))(count, 0, 1);
    if (!channel) {
        release(&preset);
        PyErr_SetString(PyExc_RuntimeError, "Channel insertion was submitted but FL returned no channel");
        return NULL;
    }
    unsigned char **items = NULL, *observed = NULL;
    int after, kind;
    void **vtable = NULL, *entry = NULL;
    if (!read_checked(list + 0x10, &after, sizeof(after)) || after != count + 1 ||
        !read_checked(list + 8, &items, sizeof(items)) || !items ||
        !read_checked(items + count, &observed, sizeof(observed)) || observed != channel ||
        !read_checked((unsigned char *)channel + 0x160, &kind, sizeof(kind)) ||
        !read_checked(channel, &vtable, sizeof(vtable)) || !vtable ||
        !read_checked(vtable + 0x150 / sizeof(void *), &entry, sizeof(entry)) || !entry ||
        (uintptr_t)entry < (uintptr_t)engine + TEXT_START ||
        (uintptr_t)entry >= (uintptr_t)engine + TEXT_END) {
        release(&preset);
        PyErr_SetString(PyExc_RuntimeError, "A channel was inserted but its identity or loader is unconfirmed");
        return NULL;
    }
    /* mode 0 loads a plugin/preset (FST); mode 1 (Sampler) loads a WAV. */
    ((LoadChannel)entry)(channel, preset, 0, 0x42, NULL, -1.0);
    release(&preset);

    wchar_t name[MAX_PLUGIN_NAME_CHARS];
    size_t name_chars = 0;
    if (!read_plugin_name((unsigned char *)channel, name, &name_chars)) {
        PyErr_SetString(PyExc_RuntimeError, "Generator plugin name readback failed layout validation");
        return NULL;
    }
    PyObject *name_obj = PyUnicode_FromWideChar(name, (Py_ssize_t)name_chars);
    if (!name_obj) return NULL;
    return Py_BuildValue("(iiN)", count, kind, name_obj);
}

/* Replaces the WAV loaded into an existing Sampler channel. This is exactly
   the load half of channel_add_sampler: locate the existing channel object,
   then call the same verified vtable +0x150 loader with mode 1 (load sample).
   No channel is inserted and no undo point is prepared here — FL's sample
   load records its own undo entry (verified by the two-step undo of
   channel_add_sampler); the caller must confirm undo restores the old path.
   Returns the channel index on success. */
PyObject *channel_replace_sample(PyObject *self, PyObject *args) {
    int index, expected_count;
    const char *path;
    if (!PyArg_ParseTuple(args, "isi:replace_sample", &index, &path, &expected_count)) return NULL;
    if (index < 0 || index >= 4096 || expected_count < 0 || expected_count >= 4096 ||
        index >= expected_count || !*path || strlen(path) > 8192) {
        PyErr_SetString(PyExc_ValueError, "Invalid channel index, expected count, or sample path");
        return NULL;
    }
    wchar_t filename[8193];
    int chars = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, filename, 8193);
    DWORD attrs = chars ? GetFileAttributesW(filename) : INVALID_FILE_ATTRIBUTES;
    if (attrs == INVALID_FILE_ATTRIBUTES || (attrs & FILE_ATTRIBUTE_DIRECTORY)) {
        PyErr_SetString(PyExc_ValueError, "Sample path does not name a readable file");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    void *thread_address = NULL;
    DWORD thread;
    if (!engine || engine != (unsigned char *)GetModuleHandleW(L"FLEngine_x64.dll") ||
        !read_checked(engine + MAIN_THREAD, &thread_address, sizeof(thread_address)) ||
        !read_checked(thread_address, &thread, sizeof(thread)) || thread != GetCurrentThreadId()) {
        PyErr_SetString(PyExc_RuntimeError, "Run the verified channel operation on the FL idle thread");
        return NULL;
    }
    unsigned char *slot = NULL, *list = NULL, *channel = NULL;
    unsigned char **items = NULL;
    int count, kind;
    void **vtable = NULL, *entry = NULL;
    if (!read_checked(engine + CHANNEL_LIST, &slot, sizeof(slot)) || !slot ||
        !read_checked(slot, &list, sizeof(list)) || !list ||
        !read_checked(list + 0x10, &count, sizeof(count)) || count != expected_count ||
        index >= count ||
        !read_checked(list + 8, &items, sizeof(items)) || !items ||
        !read_checked(items + index, &channel, sizeof(channel)) || !channel ||
        !read_checked((unsigned char *)channel + 0x160, &kind, sizeof(kind)) || kind != 0 ||
        !read_checked(channel, &vtable, sizeof(vtable)) || !vtable ||
        !read_checked(vtable + 0x150 / sizeof(void *), &entry, sizeof(entry)) || !entry ||
        (uintptr_t)entry < (uintptr_t)engine + TEXT_START ||
        (uintptr_t)entry >= (uintptr_t)engine + TEXT_END) {
        PyErr_SetString(PyExc_ValueError,
                        "Channel list changed, or index is not a Sampler with a valid loader; read again");
        return NULL;
    }
    MakeString make = (MakeString)(engine + FROM_UTF8);
    FreeString release = (FreeString)(engine + FREE_STRING);
    void *sample = NULL;
    make(&sample, path);
    if (!sample) {
        PyErr_SetString(PyExc_RuntimeError, "FL could not construct the sample path");
        return NULL;
    }
    ((LoadChannel)entry)(channel, sample, 1, 0x42, NULL, -1.0);
    release(&sample);
    return PyLong_FromLong(index);
}
