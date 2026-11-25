#include "native.h"
#include "profiles/fl_26_1_7_5654.h"
#include <string.h>

/* Piano-roll note storage: a static per-pattern record array (stride 0xC0,
   indexed by the 1-based pattern number) whose +0x28 slot holds the pattern's
   note collection. The collection is a dynamic array of 24-byte records with
   the same header family as the Playlist clip collection: data at +0x08,
   record stride at +0x10 (0x18), count at +0x14. */

enum {
    NOTE_STRIDE = 0x18,
    NOTE_PAGE = 0x18 * 48,
    PATTERN_RECORD_STRIDE = 0xC0,
    MAX_PATTERN_INDEX = 999,
    MAX_NOTES = 100000,
    MAX_ADD_BATCH = 512,
};

static int read_checked(const void *address, void *out, size_t size) {
    MEMORY_BASIC_INFORMATION info;
    if (!address || !VirtualQuery(address, &info, sizeof(info)) ||
        info.State != MEM_COMMIT || (info.Protect & (PAGE_NOACCESS | PAGE_GUARD))) {
        return 0;
    }
    uintptr_t offset = (uintptr_t)address - (uintptr_t)info.BaseAddress;
    if (offset > info.RegionSize || size > info.RegionSize - offset) {
        return 0;
    }
    memcpy(out, address, size);
    return 1;
}

static int on_idle_thread(unsigned char *engine) {
    void *thread_address = NULL;
    DWORD main_thread;
    if (!engine || !read_checked(engine + MAIN_THREAD, &thread_address, sizeof(thread_address)) ||
        !read_checked(thread_address, &main_thread, sizeof(main_thread)) ||
        main_thread != GetCurrentThreadId()) {
        PyErr_SetString(PyExc_RuntimeError, "Read the verified notes on the FL idle thread");
        return 0;
    }
    return 1;
}

/* Resolves the note collection for a 1-based pattern number. Returns 1 with a
   valid collection, 0 with count 0 when the pattern has no note store yet, and
   sets a Python exception on structural failure. */
static int note_collection(unsigned char *engine, int pattern, unsigned char **collection,
                          int32_t *count, int allow_missing) {
    unsigned char *array = engine + PATTERN_RECORD_ARRAY;
    unsigned char *recorder = NULL;
    int32_t stride = 0;
    if (pattern < 1 || pattern > MAX_PATTERN_INDEX) {
        PyErr_SetString(PyExc_ValueError, "Pattern numbers are 1..999");
        return 0;
    }
    if (!read_checked(array + (uintptr_t)pattern * PATTERN_RECORD_STRIDE + 0x28,
                      &recorder, sizeof(recorder))) {
        PyErr_SetString(PyExc_RuntimeError, "The pattern record array is unreadable");
        return 0;
    }
    if (!recorder) {
        if (allow_missing) {
            *collection = NULL;
            *count = 0;
            return 1;
        }
        PyErr_SetString(PyExc_ValueError, "The pattern has no note store yet");
        return 0;
    }
    if (!read_checked(recorder + 0x10, &stride, sizeof(stride)) ||
        !read_checked(recorder + 0x14, count, sizeof(*count)) || stride != NOTE_STRIDE ||
        *count < 0 || *count > MAX_NOTES) {
        PyErr_SetString(PyExc_RuntimeError, "The note collection failed layout validation");
        return 0;
    }
    *collection = recorder;
    return 1;
}

PyObject *pattern_read_notes(PyObject *self, PyObject *args) {
    int pattern, offset, limit;
    if (!PyArg_ParseTuple(args, "iii:read_notes", &pattern, &offset, &limit)) {
        return NULL;
    }
    if (offset < 0 || limit < 1 || limit > 48) {
        PyErr_SetString(PyExc_ValueError, "Invalid note page");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    if (!on_idle_thread(engine)) {
        return NULL;
    }
    unsigned char *collection = NULL;
    int32_t count = 0;
    if (!note_collection(engine, pattern, &collection, &count, 1)) {
        return NULL;
    }
    unsigned char *data = NULL;
    if (collection && count > 0) {
        if (!read_checked(collection + 8, &data, sizeof(data)) || !data) {
            PyErr_SetString(PyExc_RuntimeError, "The note array is unreadable");
            return NULL;
        }
    }
    int available = offset < count ? count - offset : 0;
    int take = available < limit ? available : limit;
    unsigned char records[NOTE_PAGE];
    if (take > 0 &&
        !read_checked(data + (uintptr_t)offset * NOTE_STRIDE, records,
                      (size_t)take * NOTE_STRIDE)) {
        PyErr_SetString(PyExc_RuntimeError, "Cannot read the requested note page");
        return NULL;
    }
    PyObject *bytes = PyBytes_FromStringAndSize((const char *)records, take * NOTE_STRIDE);
    if (!bytes) {
        return NULL;
    }
    return Py_BuildValue("(iiiN)", pattern, count, offset, bytes);
}

/* Appends complete note records through the engine's record-note alias.
   Arguments per note: (start_tick, channel, length_tick, key, fine_pitch,
   release, velocity). The engine packs the fixed fields itself. */
PyObject *pattern_add_notes(PyObject *self, PyObject *args) {
    int pattern, expected_count, commit, batch;
    PyObject *flat;
    if (!PyArg_ParseTuple(args, "iiiOi:add_notes", &pattern, &expected_count,
                          &commit, &flat, &batch)) {
        return NULL;
    }
    if (batch < 1 || batch > MAX_ADD_BATCH || expected_count < 0 ||
        expected_count > MAX_NOTES || (commit != 0 && commit != 1)) {
        PyErr_SetString(PyExc_ValueError, "Invalid note batch");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    if (!on_idle_thread(engine)) {
        return NULL;
    }
    unsigned char *collection = NULL;
    int32_t count = 0;
    if (!note_collection(engine, pattern, &collection, &count, 1)) {
        return NULL;
    }
    if (collection && count != expected_count) {
        PyErr_SetString(PyExc_ValueError, "The pattern's note count changed; read it again");
        return NULL;
    }
    if (!collection && expected_count != 0) {
        PyErr_SetString(PyExc_ValueError, "The pattern's note count changed; read it again");
        return NULL;
    }

    /* Validate the batch before touching anything. Each note contributes seven
       integers to the flat tuple. */
    struct { int start, channel, length, key, pitch, release, velocity; } items[MAX_ADD_BATCH];
    for (int i = 0; i < batch; i++) {
        long values[7];
        for (int f = 0; f < 7; f++) {
            PyObject *item = PyTuple_GetItem(flat, (Py_ssize_t)i * 7 + f);
            if (!item) {
                return NULL;
            }
            values[f] = PyLong_AsLong(item);
            if (PyErr_Occurred()) {
                return NULL;
            }
        }
        if (values[0] < 0 || values[2] < 1 || values[0] > 0x7ffffffe - values[2] ||
            values[1] < 0 || values[1] > 0xFFFF || values[3] < 0 || values[3] > 131 ||
            values[4] < 0 || values[4] > 0xFFFF || values[5] < 0 || values[5] > 255 ||
            values[6] < 0 || values[6] > 127) {
            PyErr_SetString(PyExc_ValueError, "Note values out of range");
            return NULL;
        }
        items[i].start = (int)values[0];
        items[i].channel = (int)values[1];
        items[i].length = (int)values[2];
        items[i].key = (int)values[3];
        items[i].pitch = (int)values[4];
        items[i].release = (int)values[5];
        items[i].velocity = (int)values[6];
    }

    if (!commit) {
        return PyLong_FromLong((long)count + batch);
    }

    /* The note store may not exist yet; create it through the engine's
       ensure helper (0x12d16e0) so the record slot and collection are both
       initialized, exactly as ensureValidNoteRecord does. */
    if (!collection) {
        void (*ensure)(int, unsigned char) =
            (void (*)(int, unsigned char))(engine + 0xed16e0);
        ensure(pattern, 1);
        if (!note_collection(engine, pattern, &collection, &count, 0)) {
            return NULL;
        }
        count = 0;
    }

    void (*record_note)(void *, unsigned int, unsigned int, unsigned int,
                        unsigned int, unsigned int, unsigned int) =
        (void (*)(void *, unsigned int, unsigned int, unsigned int,
                  unsigned int, unsigned int, unsigned int))(engine + RECORD_NOTE_ON);
    void (*commit_recorder)(void *) = NULL;
    void **vtable = NULL;
    if (!read_checked(collection, &vtable, sizeof(vtable)) || !vtable ||
        !read_checked(vtable + 3, &commit_recorder, sizeof(commit_recorder)) ||
        !commit_recorder) {
        PyErr_SetString(PyExc_RuntimeError, "The note collection has no commit entry");
        return NULL;
    }
    for (int i = 0; i < batch; i++) {
        record_note(collection, (unsigned int)items[i].start,
                    ((unsigned int)items[i].channel << 16) | 0x4000u,
                    (unsigned int)items[i].length, (unsigned int)items[i].key,
                    ((unsigned int)items[i].release << 16) | (unsigned int)items[i].pitch,
                    0x80800040u | ((unsigned int)items[i].velocity << 8));
    }
    commit_recorder(collection);
    void (*rebuild)(int, unsigned char) = (void (*)(int, unsigned char))(engine + REBUILD_PATTERN);
    rebuild(pattern, 1);

    int32_t after = 0;
    if (!read_checked(collection + 0x14, &after, sizeof(after)) ||
        after != count + (int32_t)batch) {
        PyErr_SetString(PyExc_RuntimeError, "The appended notes were not confirmed");
        return NULL;
    }
    return PyLong_FromLong(after);
}

/* Rewrites the pattern's note array in place from a caller-constructed
   snapshot. Only shrink or same-size rewrites are accepted here; appends go
   through pattern_add_notes. The expected records must match FL's live array
   byte for byte, so concurrent edits are refused before any write. */
PyObject *pattern_write_notes(PyObject *self, PyObject *args) {
    int pattern, commit;
    const char *expected;
    Py_ssize_t expected_size;
    const char *replacement;
    Py_ssize_t replacement_size;
    if (!PyArg_ParseTuple(args, "iy#y#i:write_notes", &pattern, &expected,
                          &expected_size, &replacement, &replacement_size, &commit)) {
        return NULL;
    }
    if (expected_size % NOTE_STRIDE || replacement_size % NOTE_STRIDE ||
        expected_size < NOTE_STRIDE ||
        replacement_size > expected_size ||
        replacement_size > (Py_ssize_t)MAX_NOTES * NOTE_STRIDE ||
        (commit != 0 && commit != 1)) {
        PyErr_SetString(PyExc_ValueError, "Invalid note rewrite");
        return NULL;
    }
    ModuleState *state = PyModule_GetState(self);
    unsigned char *engine = state->engine;
    if (!on_idle_thread(engine)) {
        return NULL;
    }
    unsigned char *collection = NULL;
    int32_t count = 0;
    if (!note_collection(engine, pattern, &collection, &count, 1)) {
        return NULL;
    }
    if (!collection || count != (int32_t)(expected_size / NOTE_STRIDE)) {
        PyErr_SetString(PyExc_ValueError, "The pattern's notes changed; read them again");
        return NULL;
    }
    unsigned char *data = NULL;
    if (!read_checked(collection + 8, &data, sizeof(data)) || !data) {
        PyErr_SetString(PyExc_RuntimeError, "The note array is unreadable");
        return NULL;
    }
    {
        unsigned char chunk[4096];
        Py_ssize_t remaining = expected_size;
        const char *want = expected;
        unsigned char *at = data;
        while (remaining > 0) {
            Py_ssize_t take = remaining < (Py_ssize_t)sizeof(chunk)
                                  ? remaining
                                  : (Py_ssize_t)sizeof(chunk);
            if (!read_checked(at, chunk, (size_t)take) ||
                memcmp(chunk, want, (size_t)take)) {
                PyErr_SetString(PyExc_ValueError,
                                "The pattern's notes changed; read them again");
                return NULL;
            }
            at += take;
            want += take;
            remaining -= take;
        }
    }
    int32_t new_count = (int32_t)(replacement_size / NOTE_STRIDE);
    if (!commit) {
        return PyLong_FromLong(new_count);
    }
    if (new_count > 0) {
        MEMORY_BASIC_INFORMATION info;
        if (!VirtualQuery(data, &info, sizeof(info)) ||
            !(info.Protect & (PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE |
                              PAGE_EXECUTE_WRITECOPY))) {
            PyErr_SetString(PyExc_RuntimeError, "The note array is not writable");
            return NULL;
        }
        memcpy(data, replacement, (size_t)replacement_size);
    }
    memcpy(collection + 0x14, &new_count, sizeof(new_count));
    void (*commit_recorder)(void *) = NULL;
    void **vtable = NULL;
    if (!read_checked(collection, &vtable, sizeof(vtable)) || !vtable ||
        !read_checked(vtable + 3, &commit_recorder, sizeof(commit_recorder)) ||
        !commit_recorder) {
        PyErr_SetString(PyExc_RuntimeError, "The note collection has no commit entry");
        return NULL;
    }
    commit_recorder(collection);
    void (*rebuild)(int, unsigned char) = (void (*)(int, unsigned char))(engine + REBUILD_PATTERN);
    rebuild(pattern, 1);
    int32_t after = 0;
    if (!read_checked(collection + 0x14, &after, sizeof(after)) || after != new_count) {
        PyErr_SetString(PyExc_RuntimeError, "The note rewrite was not confirmed");
        return NULL;
    }
    return PyLong_FromLong(after);
}
